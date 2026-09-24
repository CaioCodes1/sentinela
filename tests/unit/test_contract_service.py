"""Casos de uso de contrato, exercitados com repositórios em memória.

Nenhum destes testes toca em banco. É o retorno prático das portas em
`app/domain/ports/`: o ciclo de vida completo de um contrato — criação,
geração de parcelas, edição, renovação encadeada e cancelamento — roda em
milissegundos e sem infraestrutura.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.entities import Client
from app.domain.enums import ContractStatus
from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.services.contract_service import ContractService
from tests.fakes import FakeUnitOfWork


@pytest.fixture
def servico(uow: FakeUnitOfWork, client_entity: Client) -> ContractService:
    uow.clients.add(client_entity)
    return ContractService(uow)


@pytest.fixture
def periodo() -> tuple[date, date]:
    inicio = date.today()
    return inicio, inicio + timedelta(days=365)


class TestCriacao:
    def test_cria_contrato_e_gera_as_parcelas_na_mesma_operacao(
        self, servico, uow, client_entity, periodo
    ) -> None:
        inicio, fim = periodo
        contrato = servico.create(
            client_id=client_entity.id,
            number="ct-2026-001",
            monthly_amount=Decimal("1500.00"),
            start_date=inicio,
            end_date=fim,
            due_day=10,
        )

        assert contrato.status is ContractStatus.ACTIVE
        assert contrato.number == "CT-2026-001"  # normalizado para maiúsculas
        assert contrato.version == 1

        parcelas = uow.installments.list_by_contract(contrato.id)
        assert len(parcelas) >= 11
        assert uow.commits == 1  # um commit para o caso de uso inteiro

    def test_registra_historico_e_auditoria(self, servico, uow, client_entity, periodo) -> None:
        inicio, fim = periodo
        contrato = servico.create(
            client_id=client_entity.id,
            number="CT-2026-002",
            monthly_amount=Decimal("100.00"),
            start_date=inicio,
            end_date=fim,
            due_day=5,
        )
        eventos = uow.contract_events.list_by_contract(contrato.id)
        assert [e.event_type for e in eventos] == ["CREATED"]
        assert "CONTRACT_CREATED" in uow.audit.actions()

    def test_numero_duplicado_e_recusado(self, servico, client_entity, periodo) -> None:
        inicio, fim = periodo
        argumentos = dict(
            client_id=client_entity.id,
            number="CT-DUP",
            monthly_amount=Decimal("100.00"),
            start_date=inicio,
            end_date=fim,
            due_day=5,
        )
        servico.create(**argumentos)
        with pytest.raises(ConflictError, match="número"):
            servico.create(**argumentos)

    def test_cliente_inexistente(self, servico, periodo) -> None:
        import uuid

        inicio, fim = periodo
        with pytest.raises(NotFoundError, match="cliente"):
            servico.create(
                client_id=uuid.uuid4(),
                number="CT-X",
                monthly_amount=Decimal("100.00"),
                start_date=inicio,
                end_date=fim,
                due_day=5,
            )

    def test_cliente_inativo_nao_recebe_contrato(self, servico, client_entity, periodo) -> None:
        client_entity.deactivate()
        inicio, fim = periodo
        with pytest.raises(BusinessRuleError, match="inativo"):
            servico.create(
                client_id=client_entity.id,
                number="CT-Y",
                monthly_amount=Decimal("100.00"),
                start_date=inicio,
                end_date=fim,
                due_day=5,
            )


class TestEdicao:
    @pytest.fixture
    def contrato(self, servico, client_entity, periodo):  # type: ignore[no-untyped-def]
        inicio, fim = periodo
        return servico.create(
            client_id=client_entity.id,
            number="CT-EDIT",
            monthly_amount=Decimal("1000.00"),
            start_date=inicio,
            end_date=fim,
            due_day=10,
        )

    def test_alteracao_sobe_a_versao_e_registra_o_diff(self, servico, uow, contrato) -> None:
        versao_anterior = contrato.version
        atualizado = servico.update(contrato.id, monthly_amount=Decimal("1200.00"))

        assert atualizado.version == versao_anterior + 1
        evento = uow.contract_events.list_by_contract(contrato.id)[-1]
        assert evento.event_type == "UPDATED"
        # O histórico guarda o antes e o depois — "contrato alterado" sozinho
        # não responde a pergunta que alguém vai fazer daqui a seis meses.
        assert evento.payload["alteracoes"]["monthly_amount"] == {
            "de": "1000.00",
            "para": "1200.00",
        }

    def test_versao_divergente_e_recusada(self, servico, contrato) -> None:
        """A trava otimista que impede a sobrescrita silenciosa."""
        with pytest.raises(ConflictError, match="alterado por outra operação"):
            servico.update(contrato.id, expected_version=99, description="qualquer")

    def test_alteracao_sem_mudanca_real_nao_sobe_a_versao(self, servico, contrato) -> None:
        """Enviar o mesmo valor não é uma alteração.

        Sem esta checagem, um cliente que reenvia o formulário inteiro geraria
        uma linha de histórico por vez, e o histórico viraria ruído.
        """
        versao = contrato.version
        atualizado = servico.update(contrato.id, monthly_amount=Decimal("1000.00"))
        assert atualizado.version == versao

    def test_prorrogar_vigencia_gera_as_parcelas_novas_sem_duplicar(
        self, servico, uow, contrato
    ) -> None:
        antes = len(uow.installments.list_by_contract(contrato.id))
        servico.update(contrato.id, end_date=contrato.end_date + timedelta(days=90))
        depois = uow.installments.list_by_contract(contrato.id)

        assert len(depois) > antes
        competencias = [p.competence for p in depois]
        assert len(competencias) == len(set(competencias))

    def test_contrato_cancelado_nao_pode_ser_editado(self, servico, contrato) -> None:
        servico.cancel(contrato.id, reason="rescisão a pedido do cliente")
        with pytest.raises(BusinessRuleError, match="CANCELLED"):
            servico.update(contrato.id, description="tentativa")


class TestRenovacao:
    @pytest.fixture
    def contrato(self, servico, client_entity):  # type: ignore[no-untyped-def]
        hoje = date.today()
        return servico.create(
            client_id=client_entity.id,
            number="CT-REN-001",
            monthly_amount=Decimal("1000.00"),
            start_date=hoje - timedelta(days=350),
            end_date=hoje + timedelta(days=15),
            due_day=10,
        )

    def test_cria_sucessor_e_encerra_o_original(self, servico, contrato) -> None:
        """Renovar **cria um contrato novo**; não estica o antigo.

        Esticar `end_date` apagaria o registro de que o período anterior
        existiu naquele valor — e o histórico do cliente mostraria um contrato
        que nunca foi assinado assim.
        """
        sucessor = servico.renew(contrato.id, term_months=12)

        assert contrato.status is ContractStatus.RENEWED
        assert contrato.renewed_to_id == sucessor.id
        assert sucessor.previous_contract_id == contrato.id
        assert sucessor.status is ContractStatus.ACTIVE
        assert sucessor.start_date == contrato.end_date + timedelta(days=1)

    def test_numero_do_sucessor_preserva_a_linhagem(self, servico, contrato) -> None:
        primeira = servico.renew(contrato.id)
        assert primeira.number == "CT-REN-001-R1"
        segunda = servico.renew(primeira.id)
        assert segunda.number == "CT-REN-001-R2"

    def test_reajuste_e_aplicado_ao_sucessor(self, servico, contrato) -> None:
        sucessor = servico.renew(contrato.id, adjustment_percent=Decimal("10"))
        assert str(sucessor.monthly_amount) == "1100.00"

    def test_sucessor_recebe_as_proprias_parcelas(self, servico, uow, contrato) -> None:
        sucessor = servico.renew(contrato.id, term_months=6)
        parcelas = uow.installments.list_by_contract(sucessor.id)
        assert len(parcelas) >= 5
        assert all(p.contract_id == sucessor.id for p in parcelas)

    def test_historico_e_escrito_nos_dois_contratos(self, servico, uow, contrato) -> None:
        """Quem abrir o contrato novo precisa ver de onde ele veio."""
        sucessor = servico.renew(contrato.id)
        origem = [e.event_type for e in uow.contract_events.list_by_contract(contrato.id)]
        destino = [e.event_type for e in uow.contract_events.list_by_contract(sucessor.id)]

        assert "RENEWED" in origem
        assert "CREATED_BY_RENEWAL" in destino

    def test_contrato_cancelado_nao_pode_ser_renovado(self, servico, contrato) -> None:
        servico.cancel(contrato.id, reason="rescisão")
        with pytest.raises(BusinessRuleError):
            servico.renew(contrato.id)

    def test_contrato_ja_renovado_nao_renova_de_novo(self, servico, contrato) -> None:
        """Sem esta guarda, o mesmo contrato geraria dois sucessores — e duas
        cobranças paralelas para o mesmo cliente."""
        servico.renew(contrato.id)
        with pytest.raises(BusinessRuleError):
            servico.renew(contrato.id)


class TestCancelamento:
    def test_registra_motivo_data_e_auditoria(self, servico, uow, client_entity, periodo) -> None:
        inicio, fim = periodo
        contrato = servico.create(
            client_id=client_entity.id,
            number="CT-CANC",
            monthly_amount=Decimal("100.00"),
            start_date=inicio,
            end_date=fim,
            due_day=5,
        )
        cancelado = servico.cancel(contrato.id, reason="inadimplência acima de 90 dias")

        assert cancelado.status is ContractStatus.CANCELLED
        assert cancelado.cancelled_at is not None
        assert cancelado.cancellation_reason == "inadimplência acima de 90 dias"
        assert "CONTRACT_CANCELLED" in uow.audit.actions()

    def test_cancelar_duas_vezes_e_recusado(self, servico, client_entity, periodo) -> None:
        inicio, fim = periodo
        contrato = servico.create(
            client_id=client_entity.id,
            number="CT-CANC2",
            monthly_amount=Decimal("100.00"),
            start_date=inicio,
            end_date=fim,
            due_day=5,
        )
        servico.cancel(contrato.id, reason="primeira vez")
        with pytest.raises(BusinessRuleError):
            servico.cancel(contrato.id, reason="segunda vez")
