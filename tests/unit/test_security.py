"""Primitivas de segurança: hash de senha e JWT.

Os testes aqui são majoritariamente **negativos** — o que o sistema recusa. Num
módulo de segurança, o caminho feliz prova pouco: quase toda implementação
quebrada também gera e valida o próprio token com sucesso. O que separa uma
implementação correta de uma perigosa é o que ela rejeita.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.security import (
    BCRYPT_MAX_BYTES,
    decode_token,
    hash_password,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    validate_password_strength,
    verify_password,
)
from app.domain.enums import Role
from app.domain.exceptions import AuthenticationError, ValidationError


class TestHashDeSenha:
    def test_hash_nao_contem_a_senha(self) -> None:
        hashed = hash_password("SenhaCorreta#2026", rounds=4)
        assert "SenhaCorreta" not in hashed
        assert hashed.startswith("$2b$")

    def test_mesma_senha_gera_hashes_diferentes(self) -> None:
        """Sal aleatório por hash.

        Sem sal, senhas iguais viram hashes iguais — e um vazamento revela
        imediatamente quais contas compartilham senha, além de permitir tabela
        arco-íris.
        """
        a = hash_password("SenhaCorreta#2026", rounds=4)
        b = hash_password("SenhaCorreta#2026", rounds=4)
        assert a != b
        assert verify_password("SenhaCorreta#2026", a)
        assert verify_password("SenhaCorreta#2026", b)

    def test_senha_errada_e_recusada(self) -> None:
        hashed = hash_password("SenhaCorreta#2026", rounds=4)
        assert verify_password("SenhaErrada#2026", hashed) is False

    def test_hash_corrompido_nao_levanta_excecao(self) -> None:
        """Registro danificado é falha de autenticação, não erro 500.

        Um 500 aqui contaria ao atacante que aquele usuário existe e está com o
        registro quebrado — informação que a mensagem genérica de login
        deliberadamente esconde.
        """
        assert verify_password("qualquer", "não-é-um-hash") is False
        assert verify_password("qualquer", "") is False

    def test_senha_acima_de_72_bytes_e_recusada(self) -> None:
        """O bcrypt **trunca** em 72 bytes, em silêncio.

        Aceitar uma senha de 200 caracteres e validar só os 72 primeiros dá ao
        usuário a impressão de uma força que ele não tem.
        """
        with pytest.raises(ValidationError, match="72"):
            hash_password("a" * (BCRYPT_MAX_BYTES + 1), rounds=4)

    def test_acentuacao_conta_em_bytes_nao_em_caracteres(self) -> None:
        """`ç` e `ã` ocupam 2 bytes em UTF-8: 40 caracteres podem passar de 72."""
        senha = "çã" * 37  # 74 caracteres, 148 bytes
        with pytest.raises(ValidationError):
            hash_password(senha, rounds=4)


class TestPoliticaDeSenha:
    def test_aceita_senha_forte(self) -> None:
        validate_password_strength("Sentinela#2026-Forte", min_length=12)

    @pytest.mark.parametrize(
        ("senha", "trecho"),
        [
            ("Curta#1a", "12 caracteres"),
            ("minusculas#2026aa", "maiúscula"),
            ("MAIUSCULAS#2026AA", "minúscula"),
            ("SemNumeroAqui#aaa", "dígito"),
            ("SemSimbolo2026aaaa", "símbolo"),
            ("Senha123456#Aa", "óbvia"),
        ],
    )
    def test_recusa_e_diz_o_que_falta(self, senha: str, trecho: str) -> None:
        with pytest.raises(ValidationError, match=trecho):
            validate_password_strength(senha, min_length=12)


class TestJwt:
    def test_ida_e_volta(self, settings: Settings) -> None:
        user_id = uuid.uuid4()
        emitido = issue_access_token(settings, subject=user_id, role=Role.OPERATOR)
        claims = decode_token(settings, emitido.token, expected_type="access")
        assert claims.subject == user_id
        assert claims.role is Role.OPERATOR
        assert claims.jti == emitido.jti

    def test_refresh_nao_e_aceito_como_access(self, settings: Settings) -> None:
        """A checagem de `typ` que costuma faltar.

        Sem ela, o refresh token — que vive sete dias — passa como credencial
        de acesso, e a expiração curta do access token vira decoração.
        """
        refresh = issue_refresh_token(
            settings, subject=uuid.uuid4(), role=Role.ADMIN, family_id=uuid.uuid4()
        )
        with pytest.raises(AuthenticationError, match="inválido"):
            decode_token(settings, refresh.token, expected_type="access")

    def test_access_nao_e_aceito_como_refresh(self, settings: Settings) -> None:
        access = issue_access_token(settings, subject=uuid.uuid4(), role=Role.ADMIN)
        with pytest.raises(AuthenticationError):
            decode_token(settings, access.token, expected_type="refresh")

    def test_assinatura_com_outro_segredo_e_recusada(self, settings: Settings) -> None:
        outro = settings.model_copy(update={"jwt_secret": settings.jwt_secret.__class__("x" * 50)})
        token = issue_access_token(outro, subject=uuid.uuid4(), role=Role.ADMIN).token
        with pytest.raises(AuthenticationError):
            decode_token(settings, token, expected_type="access")

    def test_algoritmo_none_e_recusado(self, settings: Settings) -> None:
        """O ataque `alg: none`, que já derrubou implementações reais.

        O atacante remove a assinatura e troca o algoritmo para `none`. Uma
        biblioteca mal configurada aceita e passa a confiar num token que
        qualquer um monta. A lista explícita de algoritmos é a defesa.
        """
        payload = {
            "sub": str(uuid.uuid4()),
            "role": "ADMIN",
            "typ": "access",
            "jti": str(uuid.uuid4()),
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "exp": int(time.time()) + 600,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        }
        forjado = jwt.encode(payload, key="", algorithm="none")
        with pytest.raises(AuthenticationError):
            decode_token(settings, forjado, expected_type="access")

    def test_audiencia_errada_e_recusada(self, settings: Settings) -> None:
        """Token válido de **outro** serviço não vale aqui.

        Sem verificar `aud`, um token emitido para outra aplicação que
        compartilhe o segredo seria aceito — o erro que transforma um sistema
        comprometido em vários.
        """
        outro = settings.model_copy(update={"jwt_audience": "outra-api"})
        token = issue_access_token(outro, subject=uuid.uuid4(), role=Role.ADMIN).token
        with pytest.raises(AuthenticationError):
            decode_token(settings, token, expected_type="access")

    def test_emissor_errado_e_recusado(self, settings: Settings) -> None:
        outro = settings.model_copy(update={"jwt_issuer": "outro-emissor"})
        token = issue_access_token(outro, subject=uuid.uuid4(), role=Role.ADMIN).token
        with pytest.raises(AuthenticationError):
            decode_token(settings, token, expected_type="access")

    def test_token_expirado_e_recusado(self, settings: Settings) -> None:
        curto = settings.model_copy(update={"access_token_ttl_minutes": 1})
        emitido = issue_access_token(curto, subject=uuid.uuid4(), role=Role.ADMIN)
        # Reescreve o `exp` para o passado, mantendo assinatura válida.
        payload = jwt.decode(
            emitido.token,
            curto.jwt_secret.get_secret_value(),
            algorithms=[curto.jwt_algorithm],
            audience=curto.jwt_audience,
            issuer=curto.jwt_issuer,
        )
        payload["exp"] = int(time.time()) - 10
        vencido = jwt.encode(
            payload, curto.jwt_secret.get_secret_value(), algorithm=curto.jwt_algorithm
        )
        with pytest.raises(AuthenticationError, match="expirado"):
            decode_token(curto, vencido, expected_type="access")

    def test_token_adulterado_e_recusado(self, settings: Settings) -> None:
        """Trocar o papel dentro do token não escala privilégio."""
        token = issue_access_token(settings, subject=uuid.uuid4(), role=Role.AUDITOR).token
        cabecalho, corpo, assinatura = token.split(".")
        adulterado = f"{cabecalho}.{corpo[:-4]}AAAA.{assinatura}"
        with pytest.raises(AuthenticationError):
            decode_token(settings, adulterado, expected_type="access")

    @pytest.mark.parametrize("lixo", ["", "a.b", "não.é.jwt", "..", "x" * 500])
    def test_texto_qualquer_nao_derruba_o_decodificador(
        self, settings: Settings, lixo: str
    ) -> None:
        with pytest.raises(AuthenticationError):
            decode_token(settings, lixo, expected_type="access")

    def test_dois_tokens_seguidos_tem_jti_diferente(self, settings: Settings) -> None:
        """`jti` único é o que permite rastrear e revogar sessão individual."""
        user_id = uuid.uuid4()
        a = issue_access_token(settings, subject=user_id, role=Role.ADMIN)
        b = issue_access_token(settings, subject=user_id, role=Role.ADMIN)
        assert a.jti != b.jti

    def test_validade_respeita_a_configuracao(self, settings: Settings) -> None:
        emitido = issue_access_token(settings, subject=uuid.uuid4(), role=Role.ADMIN)
        claims = decode_token(settings, emitido.token, expected_type="access")
        duracao = claims.expires_at - claims.issued_at
        assert duracao == timedelta(minutes=settings.access_token_ttl_minutes)


class TestHashDeRefreshToken:
    def test_e_deterministico_e_nao_reversivel(self) -> None:
        token = "refresh-token-de-exemplo"
        assert hash_refresh_token(token) == hash_refresh_token(token)
        assert token not in hash_refresh_token(token)
        assert len(hash_refresh_token(token)) == 64

    def test_tokens_diferentes_geram_hashes_diferentes(self) -> None:
        assert hash_refresh_token("a") != hash_refresh_token("b")
