"""RBAC: a matriz de permissões, num arquivo só.

Decisão central: **o papel não é consultado dentro do endpoint**. O endpoint
declara qual permissão exige; o mapa abaixo é a única coisa que traduz papel em
permissões. Espalhar `if user.role == Role.ADMIN` pelos handlers é como a
autorização apodrece — ninguém consegue responder "o que o Operador pode fazer?"
sem varrer o projeto inteiro, e a resposta muda a cada endpoint novo que alguém
esquece de proteger.

Três consequências que só existem por causa dessa centralização:

1. A matriz é testável sozinha (`tests/unit/test_permissions.py`), inclusive a
   propriedade "AUDITOR não tem nenhuma permissão de escrita".
2. `GET /api/v1/admin/permissions` publica a matriz viva, não um documento que
   envelhece.
3. Um endpoint novo sem `Depends(require(...))` é visível: o teste
   `test_todas_rotas_protegidas` varre o app e reprova rota sem dependência de
   autorização.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.enums import Role


class Permission(StrEnum):
    CLIENT_CREATE = "client:create"
    CLIENT_READ = "client:read"
    CLIENT_UPDATE = "client:update"
    CLIENT_DEACTIVATE = "client:deactivate"

    CONTRACT_CREATE = "contract:create"
    CONTRACT_READ = "contract:read"
    CONTRACT_UPDATE = "contract:update"
    CONTRACT_CANCEL = "contract:cancel"
    CONTRACT_RENEW = "contract:renew"

    INSTALLMENT_READ = "installment:read"
    INSTALLMENT_SETTLE = "installment:settle"

    NOTIFICATION_READ = "notification:read"
    NOTIFICATION_RETRY = "notification:retry"

    AUDIT_READ = "audit:read"

    JOB_READ = "job:read"
    JOB_RUN = "job:run"

    USER_CREATE = "user:create"
    USER_READ = "user:read"


# Permissões que alteram estado. Serve à regra "AUDITOR nunca escreve", que é
# verificada por teste em vez de confiada à revisão de código.
WRITE_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.CLIENT_CREATE,
        Permission.CLIENT_UPDATE,
        Permission.CLIENT_DEACTIVATE,
        Permission.CONTRACT_CREATE,
        Permission.CONTRACT_UPDATE,
        Permission.CONTRACT_CANCEL,
        Permission.CONTRACT_RENEW,
        Permission.INSTALLMENT_SETTLE,
        Permission.NOTIFICATION_RETRY,
        Permission.JOB_RUN,
        Permission.USER_CREATE,
    }
)

READ_PERMISSIONS: frozenset[Permission] = frozenset(set(Permission) - WRITE_PERMISSIONS)

_OPERATOR: frozenset[Permission] = frozenset(
    {
        Permission.CLIENT_CREATE,
        Permission.CLIENT_READ,
        Permission.CLIENT_UPDATE,
        Permission.CLIENT_DEACTIVATE,
        Permission.CONTRACT_CREATE,
        Permission.CONTRACT_READ,
        Permission.CONTRACT_UPDATE,
        Permission.CONTRACT_CANCEL,
        Permission.CONTRACT_RENEW,
        Permission.INSTALLMENT_READ,
        Permission.INSTALLMENT_SETTLE,
        Permission.NOTIFICATION_READ,
        Permission.NOTIFICATION_RETRY,
        Permission.JOB_READ,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # ADMIN recebe tudo o que existe. Note que "apagar auditoria" não está na
    # lista porque **não existe** como permissão: a imutabilidade da trilha é
    # garantida por gatilho no banco, não por RBAC. Se fosse só RBAC, bastaria
    # promover alguém a ADMIN para apagar o próprio rastro.
    Role.ADMIN: frozenset(Permission),
    # OPERADOR é o dia a dia: cuida de cliente, contrato e cobrança, e pode
    # reenfileirar uma notificação que falhou. O que ele **não** tem:
    # - AUDIT_READ, porque a trilha registra os atos dele (quem é auditado não
    #   decide o que a auditoria mostra);
    # - JOB_RUN, porque disparar a automação fora de hora manda e-mail de
    #   verdade para cliente de verdade;
    # - USER_CREATE, que é escalada de privilégio por definição.
    Role.OPERATOR: _OPERATOR,
    # AUDITOR enxerga tudo e não escreve nada. É o único papel com AUDIT_READ,
    # e a interseção dele com WRITE_PERMISSIONS é vazia — verificado em teste.
    Role.AUDITOR: READ_PERMISSIONS,
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_has(role: Role, permission: Permission) -> bool:
    return permission in permissions_for(role)


def describe_matrix() -> dict[str, list[str]]:
    """Forma serializável da matriz, para o endpoint de administração."""
    return {role.value: sorted(p.value for p in perms) for role, perms in ROLE_PERMISSIONS.items()}
