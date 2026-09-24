"""O adaptador do serviço externo: timeout, retry, disjuntor e idempotência.

As respostas do provedor são interceptadas com `respx`, o que permite roteirizar
timeout, 503 e 400 sem depender de rede — e, principalmente, sem depender da
sorte. Este é o código que mais importa testar num sistema corporativo: o
caminho feliz de uma integração funciona sozinho; o que quebra em produção é o
resto.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import Settings
from app.domain.ports.notifier import NotificationRequest
from app.infrastructure.gateways.http_notifier import (
    CircuitState,
    HttpNotificationGateway,
)

BASE = "http://provedor.teste"
URL = f"{BASE}/v1/messages"


@pytest.fixture
def notifier_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "notifier_base_url": BASE,
            "notifier_timeout_seconds": 0.5,
            "notifier_max_attempts": 3,
            "notifier_backoff_base_seconds": 0.001,
            "notifier_circuit_failure_threshold": 3,
            "notifier_circuit_reset_seconds": 0.2,
        }
    )


@pytest.fixture
def pedido() -> NotificationRequest:
    return NotificationRequest(
        channel="email",
        recipient="cliente@exemplo.com",
        subject="Contrato CT-001 vence em 7 dias",
        body="corpo",
        idempotency_key="CONTRACT_EXPIRING:abc:d7",
    )


class TestCaminhoFeliz:
    @respx.mock
    def test_entrega_e_devolve_o_id_do_provedor(self, notifier_settings, pedido) -> None:
        respx.post(URL).mock(return_value=httpx.Response(202, json={"id": "msg_123"}))
        gateway = HttpNotificationGateway(notifier_settings)
        resultado = gateway.send(pedido)

        assert resultado.delivered is True
        assert resultado.provider_message_id == "msg_123"
        assert resultado.attempts == 1

    @respx.mock
    def test_envia_chave_de_idempotencia_e_autorizacao(self, notifier_settings, pedido) -> None:
        """A chave de idempotência é o que impede o retry de duplicar a mensagem.

        Sem ela, uma entrega que deu certo mas cuja resposta se perdeu no
        caminho é reenviada — e o cliente recebe o mesmo aviso duas vezes.
        """
        rota = respx.post(URL).mock(return_value=httpx.Response(202, json={"id": "m"}))
        HttpNotificationGateway(notifier_settings).send(pedido)

        enviado = rota.calls.last.request
        assert enviado.headers["Idempotency-Key"] == pedido.idempotency_key
        assert enviado.headers["Authorization"].startswith("Bearer ")
        assert "X-Request-Id" in enviado.headers

    @respx.mock
    def test_resposta_fora_do_contrato_nao_vira_falha(self, notifier_settings, pedido) -> None:
        """Corpo sem JSON num 202 ainda é entrega bem-sucedida.

        Explodir aqui marcaria como falha um envio que **deu certo** — e o
        retry mandaria a mensagem de novo.
        """
        respx.post(URL).mock(return_value=httpx.Response(202, text="ok, sem json"))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.delivered is True
        assert resultado.provider_message_id is None


class TestRetry:
    @respx.mock
    def test_repete_apos_erro_temporario_e_acerta(self, notifier_settings, pedido) -> None:
        respx.post(URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(202, json={"id": "msg_ok"}),
            ]
        )
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.delivered is True
        assert resultado.attempts == 3

    @respx.mock
    def test_desiste_apos_o_maximo_de_tentativas(self, notifier_settings, pedido) -> None:
        rota = respx.post(URL).mock(return_value=httpx.Response(503))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.delivered is False
        assert resultado.retryable is True
        assert rota.call_count == notifier_settings.notifier_max_attempts

    @respx.mock
    def test_erro_permanente_nao_e_repetido(self, notifier_settings, pedido) -> None:
        """400 é o nosso dado, não o provedor.

        Repetir um destinatário inválido três vezes queima o orçamento de
        tentativas sem nenhuma chance de sucesso — e, num lote grande, atrasa
        todas as mensagens boas que vêm depois.
        """
        rota = respx.post(URL).mock(return_value=httpx.Response(400))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.delivered is False
        assert resultado.retryable is False
        assert rota.call_count == 1

    @respx.mock
    def test_401_tambem_e_permanente(self, notifier_settings, pedido) -> None:
        rota = respx.post(URL).mock(return_value=httpx.Response(401))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.retryable is False
        assert rota.call_count == 1

    @respx.mock
    def test_timeout_e_tratado_como_temporario(self, notifier_settings, pedido) -> None:
        rota = respx.post(URL).mock(side_effect=httpx.ReadTimeout("demorou"))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert resultado.delivered is False
        assert resultado.retryable is True
        assert resultado.error == "timeout"
        assert rota.call_count == notifier_settings.notifier_max_attempts

    @respx.mock
    def test_falha_de_conexao_nao_vaza_a_url(self, notifier_settings, pedido) -> None:
        """A mensagem de erro do httpx pode conter a URL, e a URL pode ter
        credencial. Só o nome da classe da exceção vai para o resultado."""
        respx.post(URL).mock(side_effect=httpx.ConnectError("falha"))
        resultado = HttpNotificationGateway(notifier_settings).send(pedido)

        assert "ConnectError" in (resultado.error or "")
        assert BASE not in (resultado.error or "")


class TestDisjuntor:
    @respx.mock
    def test_abre_apos_o_limite_e_recusa_sem_chamar(self, notifier_settings, pedido) -> None:
        """Provedor caído não pode travar o lote inteiro.

        Sem disjuntor, 500 notificações contra um serviço fora do ar viram
        1.500 tentativas de vários segundos cada — horas de job travado
        enquanto o resto da automação não roda.
        """
        rota = respx.post(URL).mock(return_value=httpx.Response(503))
        gateway = HttpNotificationGateway(notifier_settings)

        gateway.send(pedido)  # 3 tentativas, estoura o limiar de 3 falhas
        chamadas_ate_abrir = rota.call_count

        recusada = gateway.send(pedido)
        assert recusada.delivered is False
        assert recusada.attempts == 0  # nem tentou
        assert recusada.retryable is True
        assert rota.call_count == chamadas_ate_abrir  # nenhuma chamada nova

    @respx.mock
    def test_meia_abertura_libera_uma_sonda_e_fecha_no_sucesso(
        self, notifier_settings, pedido
    ) -> None:
        import time

        respx.post(URL).mock(return_value=httpx.Response(503))
        gateway = HttpNotificationGateway(notifier_settings)
        gateway.send(pedido)
        assert gateway._breaker.state is CircuitState.OPEN

        time.sleep(notifier_settings.notifier_circuit_reset_seconds + 0.05)
        respx.post(URL).mock(return_value=httpx.Response(202, json={"id": "m"}))

        resultado = gateway.send(pedido)
        assert resultado.delivered is True
        assert gateway._breaker.state is CircuitState.CLOSED

    @respx.mock
    def test_erro_permanente_nao_abre_o_disjuntor(self, notifier_settings, pedido) -> None:
        """Um lote de e-mails inválidos não pode bloquear as notificações boas.

        O provedor está de pé e respondendo — o problema é o nosso dado.
        Contar 400 como falha do provedor abriria o disjuntor por engano.
        """
        respx.post(URL).mock(return_value=httpx.Response(400))
        gateway = HttpNotificationGateway(notifier_settings)

        for _ in range(10):
            gateway.send(pedido)

        assert gateway._breaker.state is CircuitState.CLOSED


class TestBackoff:
    def test_cresce_exponencialmente_e_tem_teto(self, notifier_settings) -> None:
        gateway = HttpNotificationGateway(notifier_settings)
        esperas = [gateway._backoff_delay(n) for n in range(1, 6)]

        assert esperas[0] < esperas[-1]
        assert all(e <= 30.0 for e in esperas)

    def test_tem_jitter(self, notifier_settings) -> None:
        """Sem jitter, todas as notificações que falharam juntas repetem
        juntas — e derrubam de novo o provedor que estava se recuperando."""
        gateway = HttpNotificationGateway(notifier_settings)
        amostras = {gateway._backoff_delay(2) for _ in range(30)}
        assert len(amostras) > 1


class TestSaude:
    @respx.mock
    def test_health_ok(self, notifier_settings) -> None:
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200))
        assert HttpNotificationGateway(notifier_settings).health() is True

    @respx.mock
    def test_health_com_falha_nao_levanta(self, notifier_settings) -> None:
        respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("caiu"))
        assert HttpNotificationGateway(notifier_settings).health() is False
