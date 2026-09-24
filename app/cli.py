"""Linha de comando administrativa.

Uso:

    python -m app.cli criar-admin
    python -m app.cli criar-usuario --email x@y.com --papel OPERATOR
    python -m app.cli rodar-job [--data 2026-09-17]
    python -m app.cli gerar-segredo
    python -m app.cli semear-demo

Por que um CLI, e não um endpoint de "inicialização": criar o primeiro
administrador por HTTP exigiria uma rota que funciona **sem autenticação** — e
uma rota dessas, esquecida aberta em produção, é uma porta para criar contas de
administrador. Aqui, quem cria o primeiro usuário precisa de acesso ao
servidor.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import generate_api_secret
from app.domain.enums import Role
from app.infrastructure.db.session import get_session_factory, init_engine
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.services.auth_service import AuthService
from app.services.client_service import ClientService
from app.services.contract_service import ContractService

logger = get_logger(__name__)


def _uow() -> SqlAlchemyUnitOfWork:
    settings = get_settings()
    init_engine(settings)
    return SqlAlchemyUnitOfWork(get_session_factory())


def criar_admin(args: argparse.Namespace) -> int:
    """Cria o administrador inicial a partir do ambiente.

    Idempotente: se já existir usuário com aquele e-mail, não faz nada e diz
    isso. Roda no início do contêiner sem risco de estourar no segundo deploy.
    """
    settings = get_settings()
    if settings.admin_initial_password is None:
        # Erro, não aviso. Na `vigencia` esta mesma situação era um WARN perdido
        # no log: a aplicação subia, nenhum usuário era criado, e quem clonava
        # o projeto passava meia hora tentando descobrir por que o login
        # respondia "credenciais inválidas" com a senha certa.
        print(
            "ERRO: ADMIN_INITIAL_PASSWORD não está definida.\n"
            "      Sem ela nenhum usuário é criado e o login será impossível.\n"
            "      Defina no .env e rode de novo.",
            file=sys.stderr,
        )
        return 1

    with _uow() as uow:
        service = AuthService(uow, settings)
        if uow.users.get_by_email(settings.admin_initial_email) is not None:
            print(f"usuário {settings.admin_initial_email} já existe; nada a fazer")
            return 0
        user = service.create_user(
            email=settings.admin_initial_email,
            password=settings.admin_initial_password.get_secret_value(),
            full_name=args.nome or "Administrador",
            role=Role.ADMIN,
        )
        print(f"administrador criado: {user.email} ({user.id})")
    return 0


def criar_usuario(args: argparse.Namespace) -> int:
    import getpass

    # A senha é lida do terminal, sem eco, e nunca vem por argumento: o que se
    # digita na linha de comando fica no histórico do shell e aparece em
    # `ps aux` para qualquer usuário da máquina enquanto o comando roda.
    senha = getpass.getpass("senha: ")
    if senha != getpass.getpass("confirme a senha: "):
        print("as senhas não conferem", file=sys.stderr)
        return 1

    with _uow() as uow:
        service = AuthService(uow, get_settings())
        user = service.create_user(
            email=args.email, password=senha, full_name=args.nome, role=Role(args.papel)
        )
        print(f"usuário criado: {user.email} ({user.role.value})")
    return 0


def rodar_job(args: argparse.Namespace) -> int:
    from app.jobs.daily_job import run_daily_check

    reference = date.fromisoformat(args.data) if args.data else None
    resultado = run_daily_check(reference=reference)
    for chave, valor in resultado.items():
        print(f"  {chave}: {valor}")
    return 0


def gerar_segredo(_: argparse.Namespace) -> int:
    print(generate_api_secret(32))
    return 0


def semear_demo(_: argparse.Namespace) -> int:
    """Popula dados de demonstração, incluindo contratos prestes a vencer.

    Os prazos são calculados a partir de hoje para que a automação tenha o que
    fazer na primeira execução — com datas fixas, o cenário de demonstração
    envelhece e o job não encontra nada.
    """
    hoje = datetime.now(UTC).date()

    with _uow() as uow:
        clientes = ClientService(uow)
        contratos = ContractService(uow)

        cliente = uow.clients.get_by_tax_id("12345678000195")
        if cliente is None:
            cliente = clientes.create(
                tax_id="12.345.678/0001-95",
                legal_name="Aurora Serviços Financeiros Ltda",
                email="financeiro@aurora.exemplo.br",
                phone="1133334444",
                trade_name="Aurora Financeira",
            )

        # Um contrato por limiar de alerta, mais um já vencido.
        cenarios = [
            ("CT-DEMO-030", 30, Decimal("2500.00"), False),
            ("CT-DEMO-007", 7, Decimal("890.50"), False),
            ("CT-DEMO-001", 1, Decimal("1200.00"), True),
            ("CT-DEMO-VEN", -3, Decimal("450.00"), False),
        ]
        criados = 0
        for numero, dias, valor, auto in cenarios:
            if uow.contracts.get_by_number(numero) is not None:
                continue
            fim = hoje + timedelta(days=dias)
            contratos.create(
                client_id=cliente.id,
                number=numero,
                monthly_amount=valor,
                start_date=fim - timedelta(days=365),
                end_date=fim,
                due_day=10,
                description=f"Contrato de demonstração vencendo em {dias} dia(s)",
                auto_renew=auto,
            )
            criados += 1

        print(f"cliente: {cliente.legal_name}")
        print(f"contratos criados: {criados}")
        print("rode `python -m app.cli rodar-job` para ver a automação agir")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(level="INFO", json_output=False)

    parser = argparse.ArgumentParser(prog="sentinela", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("criar-admin", help="cria o administrador inicial")
    p.add_argument("--nome", default="Administrador")
    p.set_defaults(func=criar_admin)

    p = sub.add_parser("criar-usuario", help="cria um usuário")
    p.add_argument("--email", required=True)
    p.add_argument("--nome", required=True)
    p.add_argument("--papel", required=True, choices=[r.value for r in Role])
    p.set_defaults(func=criar_usuario)

    p = sub.add_parser("rodar-job", help="executa a verificação diária agora")
    p.add_argument("--data", help="data de referência (AAAA-MM-DD); o padrão é hoje")
    p.set_defaults(func=rodar_job)

    p = sub.add_parser("gerar-segredo", help="gera um valor seguro para JWT_SECRET")
    p.set_defaults(func=gerar_segredo)

    p = sub.add_parser("semear-demo", help="popula dados de demonstração")
    p.set_defaults(func=semear_demo)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
