"""Casos de uso de contratos: criar, editar, cancelar, renovar.

Cada método é uma transação inteira — contrato, parcelas, evento de histórico e
auditoria vão juntos ou não vão. O `commit` aparece uma vez por caso de uso, no
fim, e nunca dentro de um laço.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from app.core.context import current_context
from app.core.logging import get_logger
from app.domain.entities import Contract, ContractEvent, Installment
from app.domain.enums import AuditAction, ClientStatus, ContractStatus
from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.domain.ports.uow import UnitOfWork
from app.domain.rules import ensure_can_be_renewed, plan_installments, plan_renewal
from app.domain.value_objects import Money
from app.services.audit_service import AuditService

logger = get_logger(__name__)


class ContractService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow
        self._audit = AuditService(uow)

    # -- Criação -----------------------------------------------------------
    def create(
        self,
        *,
        client_id: uuid.UUID,
        number: str,
        monthly_amount: Decimal | str,
        start_date: date,
        end_date: date,
        due_day: int,
        description: str | None = None,
        auto_renew: bool = False,
        renewal_term_months: int = 12,
        created_by_id: uuid.UUID | None = None,
    ) -> Contract:
        client = self._uow.clients.get(client_id)
        if client is None:
            raise NotFoundError("cliente não encontrado")
        if client.status is not ClientStatus.ACTIVE:
            raise BusinessRuleError("não é possível criar contrato para cliente inativo")

        if self._uow.contracts.get_by_number(number) is not None:
            raise ConflictError("já existe contrato com este número")

        contract = Contract(
            client_id=client_id,
            number=number,
            monthly_amount=Money.parse(monthly_amount),
            start_date=start_date,
            end_date=end_date,
            due_day=due_day,
            description=description,
            auto_renew=auto_renew,
            renewal_term_months=renewal_term_months,
            created_by_id=created_by_id,
        )
        self._uow.contracts.add(contract)
        # `flush` antes de gerar parcelas: elas referenciam `contract.id` por
        # chave estrangeira, e o insert em lote acontece fora da sessão do ORM.
        # Sem o flush, o banco recusaria por contrato inexistente.
        self._uow.flush()

        created = self._generate_installments(contract)

        self._uow.contract_events.add(
            ContractEvent(
                contract_id=contract.id,
                event_type="CREATED",
                payload={
                    "numero": contract.number,
                    "vigencia": f"{start_date.isoformat()} a {end_date.isoformat()}",
                    "mensalidade": str(contract.monthly_amount),
                    "parcelas_geradas": created,
                },
                actor_user_id=current_context().actor_user_id,
            )
        )
        self._audit.record(
            action=AuditAction.CONTRACT_CREATED,
            resource_type="contract",
            resource_id=contract.id,
            details={
                "numero": contract.number,
                "cliente": str(client_id),
                "parcelas_geradas": created,
            },
        )
        self._uow.commit()
        return contract

    # -- Consulta ----------------------------------------------------------
    def get(self, contract_id: uuid.UUID) -> Contract:
        contract = self._uow.contracts.get(contract_id)
        if contract is None:
            raise NotFoundError("contrato não encontrado")
        return contract

    def search(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: ContractStatus | None = None,
        expiring_until: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Contract], int]:
        return self._uow.contracts.search(
            client_id=client_id,
            status=status,
            expiring_until=expiring_until,
            limit=limit,
            offset=offset,
        )

    def history(self, contract_id: uuid.UUID, *, limit: int = 100) -> list[ContractEvent]:
        self.get(contract_id)  # 404 antes de devolver lista vazia
        return self._uow.contract_events.list_by_contract(contract_id, limit=limit)

    def installments(self, contract_id: uuid.UUID) -> list[Installment]:
        self.get(contract_id)
        return self._uow.installments.list_by_contract(contract_id)

    # -- Edição ------------------------------------------------------------
    def update(
        self,
        contract_id: uuid.UUID,
        *,
        expected_version: int | None = None,
        description: str | None = None,
        monthly_amount: Decimal | str | None = None,
        end_date: date | None = None,
        due_day: int | None = None,
        auto_renew: bool | None = None,
        renewal_term_months: int | None = None,
    ) -> Contract:
        contract = self.get(contract_id)

        # Trava otimista explícita: o cliente informa a versão que leu. Sem
        # isso, duas edições simultâneas resultam na última sobrescrevendo a
        # primeira em silêncio — o operador que reajustou o valor descobre
        # semanas depois que a alteração dele sumiu.
        if expected_version is not None and expected_version != contract.version:
            raise ConflictError(
                "o contrato foi alterado por outra operação",
                details={"versao_atual": contract.version, "versao_enviada": expected_version},
            )

        changes = contract.update(
            description=description,
            monthly_amount=Money.parse(monthly_amount) if monthly_amount is not None else None,
            end_date=end_date,
            due_day=due_day,
            auto_renew=auto_renew,
            renewal_term_months=renewal_term_months,
        )
        if not changes:
            return contract

        # Prorrogar a vigência ou mudar o dia de vencimento cria competências
        # que ainda não têm parcela. A inserção é idempotente, então as
        # existentes não são duplicadas nem sobrescritas — parcela já paga não
        # pode ser alterada por uma edição de contrato.
        if "end_date" in changes or "due_day" in changes:
            self._generate_installments(contract)

        self._uow.contract_events.add(
            ContractEvent(
                contract_id=contract.id,
                event_type="UPDATED",
                payload={"alteracoes": changes},
                actor_user_id=current_context().actor_user_id,
            )
        )
        self._audit.record(
            action=AuditAction.CONTRACT_UPDATED,
            resource_type="contract",
            resource_id=contract.id,
            details={"alteracoes": changes, "versao": contract.version},
        )
        self._uow.commit()
        return contract

    # -- Cancelamento ------------------------------------------------------
    def cancel(
        self, contract_id: uuid.UUID, *, reason: str, now: datetime | None = None
    ) -> Contract:
        contract = self.get(contract_id)
        contract.cancel(reason, now=now)

        self._uow.contract_events.add(
            ContractEvent(
                contract_id=contract.id,
                event_type="CANCELLED",
                payload={"motivo": contract.cancellation_reason},
                actor_user_id=current_context().actor_user_id,
            )
        )
        self._audit.record(
            action=AuditAction.CONTRACT_CANCELLED,
            resource_type="contract",
            resource_id=contract.id,
            details={"motivo": contract.cancellation_reason, "numero": contract.number},
        )
        self._uow.commit()
        return contract

    # -- Renovação ---------------------------------------------------------
    def renew(
        self,
        contract_id: uuid.UUID,
        *,
        term_months: int | None = None,
        adjustment_percent: Decimal | None = None,
        created_by_id: uuid.UUID | None = None,
    ) -> Contract:
        """Renovar **cria um contrato novo**; não estica o antigo.

        Esticar a data de término do contrato existente seria mais simples e
        estaria errado: o contrato original tem um período, um valor e um
        conjunto de parcelas que de fato existiram. Reescrever `end_date`
        apagaria o registro de que aquele período terminou naquele valor — e o
        histórico do cliente passaria a mostrar um contrato de cinco anos que
        nunca foi assinado assim.

        O antigo vai para RENEWED apontando para o sucessor, e o sucessor
        aponta de volta. A cadeia fica navegável nos dois sentidos.
        """
        contract = self.get(contract_id)
        ensure_can_be_renewed(contract.status)

        client = self._uow.clients.get(contract.client_id)
        if client is None or client.status is not ClientStatus.ACTIVE:
            raise BusinessRuleError("não é possível renovar contrato de cliente inativo")

        plan = plan_renewal(
            current_end_date=contract.end_date,
            term_months=term_months or contract.renewal_term_months,
            monthly_amount=contract.monthly_amount,
            adjustment_percent=adjustment_percent,
        )

        successor = Contract(
            client_id=contract.client_id,
            number=self._next_renewal_number(contract.number),
            monthly_amount=plan.monthly_amount,
            start_date=plan.start_date,
            end_date=plan.end_date,
            due_day=contract.due_day,
            description=contract.description,
            auto_renew=contract.auto_renew,
            renewal_term_months=plan.term_months,
            created_by_id=created_by_id,
        )
        successor.previous_contract_id = contract.id
        self._uow.contracts.add(successor)
        self._uow.flush()

        contract.mark_renewed(successor.id)
        created = self._generate_installments(successor)

        payload = {
            "sucessor": str(successor.id),
            "numero_sucessor": successor.number,
            "vigencia": f"{plan.start_date.isoformat()} a {plan.end_date.isoformat()}",
            "mensalidade_anterior": str(contract.monthly_amount),
            "mensalidade_nova": str(plan.monthly_amount),
            "reajuste_percentual": str(adjustment_percent) if adjustment_percent else "0",
            "parcelas_geradas": created,
        }
        self._uow.contract_events.add(
            ContractEvent(
                contract_id=contract.id,
                event_type="RENEWED",
                payload=payload,
                actor_user_id=current_context().actor_user_id,
            )
        )
        # O evento também é escrito no sucessor: quem abrir o contrato novo
        # precisa ver de onde ele veio sem ter que procurar no antigo.
        self._uow.contract_events.add(
            ContractEvent(
                contract_id=successor.id,
                event_type="CREATED_BY_RENEWAL",
                payload={"origem": str(contract.id), "numero_origem": contract.number},
                actor_user_id=current_context().actor_user_id,
            )
        )
        self._audit.record(
            action=AuditAction.CONTRACT_RENEWED,
            resource_type="contract",
            resource_id=contract.id,
            details=payload,
        )
        self._uow.commit()
        return successor

    # -- Mensalidades ------------------------------------------------------
    def settle_installment(self, installment_id: uuid.UUID) -> Installment:
        installment = self._uow.installments.get(installment_id)
        if installment is None:
            raise NotFoundError("parcela não encontrada")
        installment.mark_paid()

        self._uow.contract_events.add(
            ContractEvent(
                contract_id=installment.contract_id,
                event_type="INSTALLMENT_SETTLED",
                payload={
                    "competencia": installment.competence,
                    "valor": str(installment.amount),
                },
                actor_user_id=current_context().actor_user_id,
            )
        )
        self._audit.record(
            action=AuditAction.CONTRACT_UPDATED,
            resource_type="installment",
            resource_id=installment.id,
            details={"competencia": installment.competence, "acao": "quitacao"},
        )
        self._uow.commit()
        return installment

    # -- Internos ----------------------------------------------------------
    def _generate_installments(self, contract: Contract) -> int:
        planned = plan_installments(
            start_date=contract.start_date,
            end_date=contract.end_date,
            due_day=contract.due_day,
            monthly_amount=contract.monthly_amount,
        )
        return self._uow.installments.add_many_ignoring_duplicates(
            [
                Installment(
                    contract_id=contract.id,
                    competence=item.competence,
                    due_date=item.due_date,
                    amount=item.amount,
                )
                for item in planned
            ]
        )

    def _next_renewal_number(self, number: str) -> str:
        """`CT-2026-001` vira `CT-2026-001-R1`, depois `-R2`.

        O número do contrato é único no banco, então o sucessor não pode
        reaproveitá-lo. O sufixo mantém a linhagem legível para um humano — que
        é quem lê número de contrato — em vez de gerar um identificador novo
        sem relação com o anterior.
        """
        base, _, suffix = number.rpartition("-R")
        if base and suffix.isdigit():
            candidate = f"{base}-R{int(suffix) + 1}"
        else:
            candidate = f"{number}-R1"

        # Colisão é improvável, mas possível se alguém criou o `-R1` à mão.
        attempt = candidate
        counter = 1
        while self._uow.contracts.get_by_number(attempt) is not None:
            counter += 1
            attempt = f"{candidate}-{counter}"
            if counter > 50:  # pragma: no cover - salvaguarda
                raise ConflictError("não foi possível gerar número para a renovação")
        return attempt
