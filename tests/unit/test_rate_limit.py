"""Limitador de taxa: janela deslizante."""

from __future__ import annotations

import threading
import time

from app.core.rate_limit import InMemorySlidingWindowLimiter


class TestJanelaDeslizante:
    def test_permite_ate_o_limite_e_barra_o_seguinte(self) -> None:
        limitador = InMemorySlidingWindowLimiter()
        for _ in range(5):
            assert limitador.check("ip:1.2.3.4", limit=5, window_seconds=60).allowed

        barrada = limitador.check("ip:1.2.3.4", limit=5, window_seconds=60)
        assert barrada.allowed is False
        assert barrada.remaining == 0
        assert barrada.retry_after_seconds > 0

    def test_chaves_diferentes_nao_se_afetam(self) -> None:
        """Um cliente que estourou o limite não pode derrubar os outros."""
        limitador = InMemorySlidingWindowLimiter()
        for _ in range(3):
            limitador.check("ip:1.1.1.1", limit=3, window_seconds=60)
        assert limitador.check("ip:1.1.1.1", limit=3, window_seconds=60).allowed is False
        assert limitador.check("ip:2.2.2.2", limit=3, window_seconds=60).allowed is True

    def test_libera_quando_a_janela_passa(self) -> None:
        limitador = InMemorySlidingWindowLimiter()
        for _ in range(2):
            assert limitador.check("k", limit=2, window_seconds=1).allowed
        assert limitador.check("k", limit=2, window_seconds=1).allowed is False
        time.sleep(1.05)
        assert limitador.check("k", limit=2, window_seconds=1).allowed is True

    def test_nao_tem_a_brecha_da_janela_fixa(self) -> None:
        """O defeito que a janela deslizante existe para evitar.

        Com janela **fixa**, um contador zerado na virada do minuto permite o
        dobro do limite concentrado em torno da virada: 5 no fim de um minuto e
        5 no início do seguinte. Aqui, como a janela desliza junto com o
        relógio, as marcas antigas só saem quando de fato envelhecem — e as
        cinco primeiras ainda contam.
        """
        limitador = InMemorySlidingWindowLimiter()
        for _ in range(5):
            limitador.check("k", limit=5, window_seconds=2)

        time.sleep(1.0)  # metade da janela: nada expirou ainda
        assert limitador.check("k", limit=5, window_seconds=2).allowed is False

    def test_reset_limpa_a_chave(self) -> None:
        limitador = InMemorySlidingWindowLimiter()
        for _ in range(3):
            limitador.check("k", limit=3, window_seconds=60)
        assert limitador.check("k", limit=3, window_seconds=60).allowed is False
        limitador.reset("k")
        assert limitador.check("k", limit=3, window_seconds=60).allowed is True


class TestConcorrencia:
    def test_nao_deixa_passar_alem_do_limite_com_threads(self) -> None:
        """Sem o lock interno, duas threads leem o mesmo tamanho de fila e
        passam as duas — justamente na chamada que deveria ser barrada.

        Os endpoints deste projeto são `def` e rodam no threadpool do FastAPI,
        então esta concorrência é real, não hipotética.
        """
        limitador = InMemorySlidingWindowLimiter()
        limite = 50
        permitidas: list[bool] = []
        trava = threading.Lock()

        def bater() -> None:
            resultado = limitador.check("compartilhada", limit=limite, window_seconds=60)
            with trava:
                permitidas.append(resultado.allowed)

        threads = [threading.Thread(target=bater) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(permitidas) == limite


class TestProtecaoDeMemoria:
    def test_respeita_o_teto_de_chaves(self) -> None:
        """O mecanismo de defesa não pode virar vetor de exaustão de memória.

        Sem teto, um atacante variando o IP de origem faz o dicionário crescer
        sem limite — derruba o processo usando exatamente o componente que
        deveria protegê-lo.
        """
        limitador = InMemorySlidingWindowLimiter(max_keys=100)
        for i in range(500):
            limitador.check(f"ip:{i}", limit=10, window_seconds=60)
        assert len(limitador._hits) <= 100
