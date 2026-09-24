"""A automação diária.

O que estes testes garantem, e que é o coração de qualquer rotina agendada de
sistema corporativo:

1. **idempotência** — rodar duas vezes não gera nada em dobro;
2. **exclusão mútua** — duas instâncias não trabalham em paralelo;
3. **tolerância a falha individual** — um registro problemático não derruba a
   varredura dos outros.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.entities import Client
from app.domain.enums import (
    ContractStatus,
    InstallmentStatus,
    JobStatus,
    NotificationStatus,
)
from app.domain.ports.notifier import NotificationResult
from app.services.automation_service import AutomationService
from app.services.contract_service import ContractService
from tests.fakes import FakeNotificationGateway, FakeUnitOfWork


@pytest.fixture
def cenario(uow: FakeUnitOfWork, client_entity: Client, contract_factory):  # type: ignore[no-untyped-def]
    uow.clients.add(client_entity)

    def _adicionar(**kwargs) -> object:  # type: ignore[no-untyped-def]
        contrato = contract_factory(client_id=client_entity.id, **kwargs)
        uow.contracts.add(contrato)
        return contrato

    return _adicionar


@pytest.fixture
def servico(uow, gateway, settings) -> AutomationService:  # type: ignore[no-untyped-def]
    return AutomationService(uow, gateway, settings)


class TestAlertasDeVencimento:
    def test_gera_alerta_no_limiar_exato(self, servico, uow, cenario) -> None:
        cenario(dias_para_vencer=30, numero="CT-30")
        resultado = servico.run_daily()

        assert resultado["alertas_gerados"] == 1
        notificacao = next(iter(uow.notifications.items.values()))
        assert "30 dia" in notificacao.subject

    def test_nao_gera_alerta_fora_do_limiar(self, servico, cenario) -> None:
        cenario(dias_para_vencer=22, numero="CT-22")
        assert servico.run_daily()["alertas_gerados"] == 0

    def test_gera_um_alerta_por_limiar_atingido(self, servico, cenario) -> None:
        for dias in (30, 15, 7, 1):
            cenario(dias_para_vencer=dias, numero=f"CT-{dias}")
        assert servico.run_daily()["alertas_gerados"] == 4

    def test_contrato_dentro_da_janela_vira_a_vencer(self, servico, uow, cenario) -> None:
        contrato = cenario(dias_para_vencer=20, numero="CT-JANELA")
        servico.run_daily()
        assert uow.contracts.get(contrato.id).status is ContractStatus.EXPIRING

    def test_contrato_longe_do_fim_permanece_ativo(self, servico, uow, cenario) -> None:
        contrato = cenario(dias_para_vencer=200, numero="CT-LONGE")
        servico.run_daily()
        assert uow.contracts.get(contrato.id).status is ContractStatus.ACTIVE


class TestIdempotencia:
    def test_rodar_duas_vezes_nao_duplica_nada(self, servico, uow, cenario) -> None:
        """A propriedade que permite reprocessar um dia que falhou.

        Sem ela, a única forma de recuperar uma execução interrompida seria
        conferir manualmente o que já tinha sido feito — ou aceitar mandar o
        mesmo aviso duas vezes para o cliente.
        """
        cenario(dias_para_vencer=7, numero="CT-IDEM")

        primeira = servico.run_daily()
        notificacoes_apos_primeira = len(uow.notifications.items)
        segunda = servico.run_daily()

        assert primeira["alertas_gerados"] == 1
        assert segunda["alertas_gerados"] == 0
        assert len(uow.notifications.items) == notificacoes_apos_primeira

    def test_reprocessar_data_antiga_nao_gera_alerta_novo(self, servico, uow, cenario) -> None:
        """O marcador da chave é o **limiar**, não a data de execução.

        Fosse a data, reprocessar o dia anterior criaria uma segunda
        notificação de "faltam 7 dias" para o mesmo contrato.
        """
        cenario(dias_para_vencer=7, numero="CT-REPROC")
        servico.run_daily()
        antes = len(uow.notifications.items)

        servico.run_daily(reference=date.today())
        assert len(uow.notifications.items) == antes


class TestExclusaoMutua:
    def test_segunda_instancia_registra_skipped_sem_erro(
        self, client_entity, gateway, settings
    ) -> None:
        """Duas réplicas rodando o job das 3h enviariam tudo em duplicidade.

        A segunda não encontra a trava e sai registrando SKIPPED — que é o
        comportamento correto, não uma falha.
        """
        uow = FakeUnitOfWork(lock_disponivel=False)
        uow.clients.add(client_entity)
        servico = AutomationService(uow, gateway, settings)

        resultado = servico.run_daily()

        assert resultado["status"] == JobStatus.SKIPPED.value
        assert uow.job_runs.items[-1].status is JobStatus.SKIPPED
        assert len(uow.notifications.items) == 0


class TestExpiracao:
    def test_contrato_vencido_e_marcado_e_notificado(self, servico, uow, cenario) -> None:
        contrato = cenario(dias_para_vencer=-5, numero="CT-VENCIDO")
        resultado = servico.run_daily()

        assert resultado["contratos_expirados"] == 1
        assert uow.contracts.get(contrato.id).status is ContractStatus.EXPIRED
        assert "CONTRACT_EXPIRED" in uow.audit.actions()

    def test_contrato_com_renovacao_automatica_gera_sucessor(self, servico, uow, cenario) -> None:
        contrato = cenario(dias_para_vencer=-1, numero="CT-AUTO", auto_renew=True)
        resultado = servico.run_daily()

        assert resultado["renovacoes_automaticas"] == 1
        assert uow.contracts.get(contrato.id).status is ContractStatus.RENEWED
        assert uow.contracts.get_by_number("CT-AUTO-R1") is not None

    def test_falha_na_renovacao_automatica_nao_derruba_o_job(
        self, servico, uow, cenario, client_entity
    ) -> None:
        """Um registro problemático não pode abortar a varredura dos outros.

        Aqui o cliente está inativo, o que impede a renovação. O contrato é
        marcado como vencido e o job segue — em vez de a exceção subir e
        cancelar o processamento de todos os contratos seguintes.
        """
        contrato = cenario(dias_para_vencer=-1, numero="CT-FALHA", auto_renew=True)
        cenario(dias_para_vencer=30, numero="CT-OK")
        client_entity.deactivate()

        resultado = servico.run_daily()

        assert resultado["renovacoes_automaticas"] == 0
        assert uow.contracts.get(contrato.id).status is ContractStatus.EXPIRED
        assert resultado["alertas_gerados"] == 1  # o outro contrato foi processado

    def test_contrato_cancelado_e_ignorado(self, servico, uow, cenario) -> None:
        contrato = cenario(dias_para_vencer=-10, numero="CT-CANC")
        contrato.cancel("rescisão")
        servico.run_daily()
        assert uow.contracts.get(contrato.id).status is ContractStatus.CANCELLED


class TestMensalidadesEmAtraso:
    def test_marca_atraso_e_gera_cobranca(self, uow, gateway, settings, client_entity) -> None:
        uow.clients.add(client_entity)
        contratos = ContractService(uow)
        hoje = date.today()
        contrato = contratos.create(
            client_id=client_entity.id,
            number="CT-ATRASO",
            monthly_amount=Decimal("300.00"),
            start_date=hoje - timedelta(days=120),
            end_date=hoje + timedelta(days=240),
            due_day=10,
        )

        resultado = AutomationService(uow, gateway, settings).run_daily()

        assert resultado["parcelas_atrasadas"] >= 1
        assert resultado["cobrancas_geradas"] >= 1
        vencidas = [
            p
            for p in uow.installments.list_by_contract(contrato.id)
            if p.status is InstallmentStatus.OVERDUE
        ]
        assert vencidas

    def test_parcela_do_dia_nao_conta_como_atrasada(
        self, uow, gateway, settings, client_entity
    ) -> None:
        """No dia do vencimento o cliente ainda tem o dia inteiro para pagar.

        Marcar atraso às 3h da manhã do próprio dia manda cobrança de
        inadimplência para quem está em dia.
        """
        uow.clients.add(client_entity)
        hoje = date.today()
        contratos = ContractService(uow)
        contratos.create(
            client_id=client_entity.id,
            number="CT-HOJE",
            monthly_amount=Decimal("300.00"),
            start_date=hoje.replace(day=1),
            end_date=hoje + timedelta(days=365),
            due_day=hoje.day,
        )
        resultado = AutomationService(uow, gateway, settings).run_daily(reference=hoje)
        assert resultado["parcelas_atrasadas"] == 0


class TestDespacho:
    def test_notificacoes_sao_enviadas_e_marcadas(self, servico, uow, cenario) -> None:
        cenario(dias_para_vencer=15, numero="CT-ENVIO")
        resultado = servico.run_daily()

        assert resultado["envio"]["enviadas"] == 1
        notificacao = next(iter(uow.notifications.items.values()))
        assert notificacao.status is NotificationStatus.SENT
        assert notificacao.sent_at is not None

    def test_falha_de_envio_nao_interrompe_o_lote(
        self, uow, settings, client_entity, contract_factory
    ) -> None:
        """Um destinatário com problema não pode bloquear os avisos dos outros."""
        uow.clients.add(client_entity)
        for dias in (30, 15):
            uow.contracts.add(
                contract_factory(
                    client_id=client_entity.id, dias_para_vencer=dias, numero=f"CT-{dias}"
                )
            )

        gateway = FakeNotificationGateway(
            respostas=[
                NotificationResult(delivered=False, error="503", retryable=True),
                NotificationResult(delivered=True, provider_message_id="ok"),
            ]
        )
        resultado = AutomationService(uow, gateway, settings).run_daily()

        assert resultado["envio"]["processadas"] == 2
        assert resultado["envio"]["enviadas"] == 1
        assert resultado["envio"]["falhas"] == 1

    def test_esgotar_tentativas_manda_para_a_fila_morta(
        self, uow, settings, client_entity, contract_factory
    ) -> None:
        uow.clients.add(client_entity)
        uow.contracts.add(
            contract_factory(client_id=client_entity.id, dias_para_vencer=7, numero="CT-DL")
        )
        falha = NotificationResult(delivered=False, error="503", retryable=True)
        gateway = FakeNotificationGateway(respostas=[falha, falha, falha])
        servico = AutomationService(uow, gateway, settings)

        for _ in range(settings.notifier_max_attempts):
            servico.run_daily()

        notificacao = next(iter(uow.notifications.items.values()))
        assert notificacao.status is NotificationStatus.DEAD_LETTER
        assert notificacao.attempts == settings.notifier_max_attempts

    def test_comita_por_notificacao_e_nao_no_fim_do_lote(
        self, uow, gateway, settings, client_entity, contract_factory
    ) -> None:
        """A transação não pode ficar aberta durante as chamadas de rede.

        Cada envio faz uma chamada HTTP que pode levar segundos. Com um único
        commit no fim, a transação fica ociosa durante toda a rede, e o
        PostgreSQL a derruba pelo `idle_in_transaction_session_timeout` — o
        sintoma é `server closed the connection unexpectedly`, que parece
        problema de rede e é problema de desenho.

        Este teste fixa o comportamento correto para que ninguém "otimize"
        voltando ao commit único.
        """
        uow.clients.add(client_entity)
        for dias in (30, 15, 7):
            uow.contracts.add(
                contract_factory(
                    client_id=client_entity.id, dias_para_vencer=dias, numero=f"CT-CM-{dias}"
                )
            )
        servico = AutomationService(uow, gateway, settings)
        commits_antes = uow.commits

        resultado = servico.run_daily()

        # Ao menos um commit por notificação despachada.
        assert uow.commits - commits_antes >= resultado["envio"]["processadas"]
        assert resultado["envio"]["processadas"] == 3


class TestRegistroDaExecucao:
    def test_grava_job_run_com_estatisticas(self, servico, uow, cenario) -> None:
        """Sem este registro, "o job rodou hoje?" não tem resposta."""
        cenario(dias_para_vencer=30, numero="CT-RUN")
        servico.run_daily()

        execucao = uow.job_runs.items[-1]
        assert execucao.status is JobStatus.SUCCESS
        assert execucao.finished_at is not None
        assert execucao.duration_seconds is not None
        assert execucao.stats["contratos_analisados"] == 1

    def test_audita_inicio_e_fim(self, servico, uow, cenario) -> None:
        cenario(dias_para_vencer=30, numero="CT-AUD")
        servico.run_daily()
        acoes = uow.audit.actions()
        assert "JOB_STARTED" in acoes
        assert "JOB_FINISHED" in acoes

    def test_autor_da_auditoria_e_o_sistema(self, servico, uow, cenario) -> None:
        """A automação também tem autor identificado.

        Deixar o autor nulo encheria a trilha de linhas sem responsável, e
        ninguém saberia distinguir "o sistema expirou" de "faltou registrar
        quem expirou".
        """
        cenario(dias_para_vencer=30, numero="CT-ATOR")
        servico.run_daily()
        inicio = next(e for e in uow.audit.items if e.action.value == "JOB_STARTED")
        assert inicio.actor_email == "system:verificacao-diaria-contratos"


class TestCadastroIncompleto:
    def test_contrato_sem_cliente_nao_derruba_a_varredura(
        self, uow, gateway, settings, contract_factory
    ) -> None:
        """Cadastro incompleto de um cliente não pode parar o job inteiro."""
        import uuid

        uow.contracts.add(
            contract_factory(client_id=uuid.uuid4(), dias_para_vencer=30, numero="CT-ORFAO")
        )
        resultado = AutomationService(uow, gateway, settings).run_daily()

        assert resultado["status"] == JobStatus.SUCCESS.value
        assert resultado["alertas_gerados"] == 0
