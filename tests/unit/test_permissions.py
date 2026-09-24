"""A matriz de RBAC.

Estes testes verificam **propriedades** da matriz, não linhas específicas. A
diferença é que uma propriedade continua valendo quando alguém acrescenta uma
permissão nova: se `Permission.CONTRACT_DELETE` for criada amanhã e entrar sem
querer no AUDITOR, `test_auditor_nunca_escreve` reprova sozinho. Um teste que
listasse as permissões esperadas do AUDITOR passaria — porque ninguém teria
atualizado a lista.
"""

from __future__ import annotations

import pytest

from app.core.permissions import (
    READ_PERMISSIONS,
    ROLE_PERMISSIONS,
    WRITE_PERMISSIONS,
    Permission,
    describe_matrix,
    permissions_for,
    role_has,
)
from app.domain.enums import Role


class TestInvariantesDaMatriz:
    def test_todo_papel_esta_na_matriz(self) -> None:
        """Papel novo sem entrada aqui receberia conjunto vazio — falha silenciosa."""
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_toda_permissao_e_de_leitura_ou_de_escrita(self) -> None:
        """Permissão nova cai numa das listas. Se não cair, este teste avisa.

        Sem esta checagem, uma permissão de escrita esquecida fora de
        `WRITE_PERMISSIONS` seria classificada como leitura e acabaria no
        AUDITOR — que é somente-leitura por definição.
        """
        assert WRITE_PERMISSIONS | READ_PERMISSIONS == set(Permission)
        assert not (WRITE_PERMISSIONS & READ_PERMISSIONS)

    def test_auditor_nunca_escreve(self) -> None:
        """A propriedade central do perfil de auditoria."""
        assert not (permissions_for(Role.AUDITOR) & WRITE_PERMISSIONS)

    def test_auditor_le_tudo(self) -> None:
        assert permissions_for(Role.AUDITOR) == READ_PERMISSIONS

    def test_admin_tem_todas(self) -> None:
        assert permissions_for(Role.ADMIN) == set(Permission)

    def test_nao_existe_permissao_de_apagar_auditoria(self) -> None:
        """A imutabilidade da trilha não é questão de RBAC.

        Se existisse `audit:delete`, bastaria promover alguém a ADMIN para
        apagar o próprio rastro. A garantia fica no gatilho do banco, e a
        permissão simplesmente não existe.
        """
        nomes = {p.value for p in Permission}
        assert not any("audit:delete" in n or "audit:update" in n for n in nomes)


class TestOperador:
    def test_faz_a_operacao_do_dia_a_dia(self) -> None:
        for permissao in (
            Permission.CLIENT_CREATE,
            Permission.CLIENT_UPDATE,
            Permission.CONTRACT_CREATE,
            Permission.CONTRACT_RENEW,
            Permission.CONTRACT_CANCEL,
            Permission.INSTALLMENT_SETTLE,
            Permission.NOTIFICATION_RETRY,
        ):
            assert role_has(Role.OPERATOR, permissao), permissao

    @pytest.mark.parametrize(
        ("permissao", "motivo"),
        [
            (Permission.AUDIT_READ, "quem é auditado não lê a própria auditoria"),
            (Permission.USER_CREATE, "criar usuário é escalada de privilégio"),
            (Permission.JOB_RUN, "disparar o job manda e-mail real para cliente real"),
        ],
    )
    def test_nao_tem_o_que_nao_deve(self, permissao: Permission, motivo: str) -> None:
        assert not role_has(Role.OPERATOR, permissao), motivo


class TestAuditor:
    def test_e_o_unico_papel_nao_admin_com_acesso_a_trilha(self) -> None:
        assert role_has(Role.AUDITOR, Permission.AUDIT_READ)
        assert role_has(Role.ADMIN, Permission.AUDIT_READ)
        assert not role_has(Role.OPERATOR, Permission.AUDIT_READ)

    @pytest.mark.parametrize("permissao", sorted(WRITE_PERMISSIONS))
    def test_nenhuma_escrita_passa(self, permissao: Permission) -> None:
        assert not role_has(Role.AUDITOR, permissao)


class TestPublicacaoDaMatriz:
    def test_descricao_bate_com_a_matriz(self) -> None:
        """O endpoint publica a matriz viva, não uma cópia que envelhece."""
        descrita = describe_matrix()
        assert set(descrita) == {r.value for r in Role}
        for papel, permissoes in ROLE_PERMISSIONS.items():
            assert descrita[papel.value] == sorted(p.value for p in permissoes)

    def test_permissoes_seguem_o_padrao_recurso_acao(self) -> None:
        for permissao in Permission:
            recurso, _, acao = permissao.value.partition(":")
            assert recurso and acao, permissao
