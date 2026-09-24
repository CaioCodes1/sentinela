"""Autenticação: login, bloqueio por tentativas e rotação de refresh token."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.security import decode_token
from app.domain.entities import User
from app.domain.enums import Role
from app.domain.exceptions import (
    AccountLockedError,
    AuthenticationError,
    ConflictError,
)
from app.services.auth_service import AuthService
from tests.fakes import FakeUnitOfWork

SENHA = "Sentinela#2026-Forte"


@pytest.fixture
def servico(uow: FakeUnitOfWork, settings: Settings, admin_user: User) -> AuthService:
    uow.users.add(admin_user)
    return AuthService(uow, settings)


@pytest.fixture
def usuario(uow: FakeUnitOfWork, settings: Settings) -> User:
    from app.core.security import hash_password
    from app.domain.value_objects import EmailAddress

    user = User(
        email=EmailAddress.parse("operador@sentinela.local"),
        password_hash=hash_password(SENHA, rounds=settings.bcrypt_rounds),
        role=Role.OPERATOR,
        full_name="Operador de Teste",
    )
    uow.users.add(user)
    return user


class TestLogin:
    def test_credenciais_corretas_devolvem_o_par_de_tokens(
        self, servico, settings, usuario
    ) -> None:
        par = servico.authenticate(email=str(usuario.email), password=SENHA)

        assert par.token_type == "Bearer"
        assert par.role is Role.OPERATOR
        assert par.expires_in == settings.access_token_ttl_minutes * 60
        claims = decode_token(settings, par.access_token, expected_type="access")
        assert claims.subject == usuario.id

    def test_email_e_insensivel_a_caixa(self, servico, usuario) -> None:
        par = servico.authenticate(email="OPERADOR@Sentinela.LOCAL", password=SENHA)
        assert par.access_token

    @pytest.mark.parametrize(
        "caso",
        ["inexistente@sentinela.local", "operador@sentinela.local"],
        ids=["usuario-inexistente", "senha-errada"],
    )
    def test_mensagem_de_erro_e_sempre_a_mesma(self, servico, usuario, caso) -> None:
        """Mensagens distintas transformariam o login num verificador de e-mails.

        "Usuário não encontrado" versus "senha incorreta" entrega ao atacante
        exatamente a lista de contas que existem.
        """
        with pytest.raises(AuthenticationError) as erro:
            servico.authenticate(email=caso, password="SenhaErrada#2026")
        assert str(erro.value) == "credenciais inválidas"

    def test_conta_desativada_da_a_mesma_mensagem(self, servico, usuario) -> None:
        usuario.is_active = False
        with pytest.raises(AuthenticationError, match="credenciais inválidas"):
            servico.authenticate(email=str(usuario.email), password=SENHA)


class TestBloqueioDeConta:
    def test_bloqueia_apos_o_limite_de_tentativas(self, servico, settings, usuario) -> None:
        for _ in range(settings.login_max_attempts):
            with pytest.raises(AuthenticationError):
                servico.authenticate(email=str(usuario.email), password="errada")

        assert usuario.is_locked() is True
        # Agora nem a senha certa entra — e a mensagem é específica, porque a
        # conta é do titular e ele precisa saber por que não consegue entrar.
        with pytest.raises(AccountLockedError, match="bloqueada"):
            servico.authenticate(email=str(usuario.email), password=SENHA)

    def test_contador_sobrevive_a_tentativa_falha(self, servico, usuario) -> None:
        """O incremento precisa ser confirmado mesmo com o fluxo terminando em
        exceção. Sem o commit explícito, o rollback zeraria o contador a cada
        erro e o bloqueio nunca aconteceria."""
        with pytest.raises(AuthenticationError):
            servico.authenticate(email=str(usuario.email), password="errada")
        assert usuario.failed_login_attempts == 1

    def test_login_correto_zera_o_contador(self, servico, usuario) -> None:
        with pytest.raises(AuthenticationError):
            servico.authenticate(email=str(usuario.email), password="errada")
        servico.authenticate(email=str(usuario.email), password=SENHA)

        assert usuario.failed_login_attempts == 0
        assert usuario.locked_until is None
        assert usuario.last_login_at is not None

    def test_bloqueio_e_auditado(self, servico, uow, settings, usuario) -> None:
        for _ in range(settings.login_max_attempts):
            with pytest.raises(AuthenticationError):
                servico.authenticate(email=str(usuario.email), password="errada")
        assert "ACCOUNT_LOCKED" in uow.audit.actions()

    def test_falha_de_login_registra_email_mascarado(self, servico, uow, usuario) -> None:
        """A trilha precisa identificar o alvo de uma força bruta sem virar
        uma lista de e-mails válidos para quem tem leitura da auditoria."""
        with pytest.raises(AuthenticationError):
            servico.authenticate(email="alvo@empresa.com", password="x")

        registro = next(e for e in uow.audit.items if e.action.value == "LOGIN_FAILURE")
        assert registro.details["email"] == "a**o@empresa.com"


class TestRotacaoDeRefreshToken:
    def _logar(self, servico, usuario):  # type: ignore[no-untyped-def]
        return servico.authenticate(email=str(usuario.email), password=SENHA)

    def test_refresh_devolve_par_novo(self, servico, usuario) -> None:
        primeiro = self._logar(servico, usuario)
        segundo = servico.refresh(primeiro.refresh_token)

        assert segundo.refresh_token != primeiro.refresh_token
        assert segundo.access_token != primeiro.access_token

    def test_token_usado_e_invalidado(self, servico, usuario) -> None:
        """Refresh token vale **uma vez**.

        Sem a rotação, um token capturado dá sete dias de acesso ao atacante
        sem deixar rastro nenhum.
        """
        primeiro = self._logar(servico, usuario)
        servico.refresh(primeiro.refresh_token)

        with pytest.raises(AuthenticationError):
            servico.refresh(primeiro.refresh_token)

    def test_reuso_revoga_a_familia_inteira(self, servico, uow, usuario) -> None:
        """A detecção de roubo.

        Um token já revogado reaparecendo só tem duas explicações: ou o ladrão
        está usando, ou o titular está usando depois do ladrão. Nos dois casos
        existe cópia na mão errada, e a sessão inteira cai.
        """
        primeiro = self._logar(servico, usuario)
        segundo = servico.refresh(primeiro.refresh_token)

        with pytest.raises(AuthenticationError, match="segurança"):
            servico.refresh(primeiro.refresh_token)  # reúso do antigo

        # O token legítimo, que ainda não tinha sido usado, também morre.
        with pytest.raises(AuthenticationError):
            servico.refresh(segundo.refresh_token)

        assert "TOKEN_REUSE_DETECTED" in uow.audit.actions()

    def test_familia_e_preservada_entre_renovacoes(self, servico, uow, usuario) -> None:
        primeiro = self._logar(servico, usuario)
        servico.refresh(primeiro.refresh_token)
        familias = {t.family_id for t in uow.refresh_tokens.items.values()}
        assert len(familias) == 1

    def test_access_token_nao_serve_para_renovar(self, servico, usuario) -> None:
        par = self._logar(servico, usuario)
        with pytest.raises(AuthenticationError):
            servico.refresh(par.access_token)

    def test_token_desconhecido_e_auditado(self, servico, uow, settings, usuario) -> None:
        """Assinatura válida sem registro no banco: token de antes de um logout
        global, ou de uma base restaurada. Não dá para identificar a família,
        então só se recusa — mas o evento fica registrado."""
        import uuid

        from app.core.security import issue_refresh_token

        forasteiro = issue_refresh_token(
            settings, subject=usuario.id, role=usuario.role, family_id=uuid.uuid4()
        )
        with pytest.raises(AuthenticationError):
            servico.refresh(forasteiro.token)
        assert "TOKEN_REUSE_DETECTED" in uow.audit.actions()


class TestLogout:
    def test_revoga_a_familia_da_sessao(self, servico, uow, usuario) -> None:
        par = servico.authenticate(email=str(usuario.email), password=SENHA)
        revogados = servico.logout(user_id=usuario.id, refresh_token=par.refresh_token)

        assert revogados == 1
        with pytest.raises(AuthenticationError):
            servico.refresh(par.refresh_token)

    def test_logout_global_derruba_todas_as_sessoes(self, servico, usuario) -> None:
        primeira = servico.authenticate(email=str(usuario.email), password=SENHA)
        segunda = servico.authenticate(email=str(usuario.email), password=SENHA)

        assert servico.logout(user_id=usuario.id, all_sessions=True) == 2
        for par in (primeira, segunda):
            with pytest.raises(AuthenticationError):
                servico.refresh(par.refresh_token)


class TestCriacaoDeUsuario:
    def test_cria_com_senha_hasheada(self, servico, uow) -> None:
        user = servico.create_user(
            email="novo@sentinela.local",
            password=SENHA,
            full_name="Usuário Novo",
            role=Role.AUDITOR,
        )
        assert user.password_hash != SENHA
        assert user.password_hash.startswith("$2b$")
        assert "USER_CREATED" in uow.audit.actions()

    def test_email_duplicado_e_recusado(self, servico, usuario) -> None:
        with pytest.raises(ConflictError, match="e-mail"):
            servico.create_user(
                email=str(usuario.email),
                password=SENHA,
                full_name="Outro",
                role=Role.OPERATOR,
            )

    def test_senha_fraca_e_recusada(self, servico) -> None:
        from app.domain.exceptions import ValidationError

        with pytest.raises(ValidationError, match="senha fraca"):
            servico.create_user(
                email="fraco@sentinela.local",
                password="123456",
                full_name="Senha Fraca",
                role=Role.OPERATOR,
            )

    def test_auditoria_nao_guarda_a_senha(self, servico, uow) -> None:
        servico.create_user(
            email="auditado@sentinela.local",
            password=SENHA,
            full_name="Auditado",
            role=Role.OPERATOR,
        )
        registro = next(e for e in uow.audit.items if e.action.value == "USER_CREATED")
        assert SENHA not in str(registro.details)


class TestTrocaDeSenha:
    def test_exige_a_senha_atual(self, servico, usuario) -> None:
        with pytest.raises(AuthenticationError, match="senha atual"):
            servico.change_password(
                user_id=usuario.id,
                current_password="errada",
                new_password="Trocada#2026-Longa",
            )

    def test_troca_encerra_todas_as_sessoes(self, servico, usuario) -> None:
        """Se a troca foi motivada por suspeita de invasão, deixar as sessões
        antigas vivas esvazia o gesto: o invasor continua dentro com o refresh
        token que já tinha."""
        par = servico.authenticate(email=str(usuario.email), password=SENHA)
        servico.change_password(
            user_id=usuario.id,
            current_password=SENHA,
            new_password="Trocada#2026-Longa",
        )
        with pytest.raises(AuthenticationError):
            servico.refresh(par.refresh_token)
