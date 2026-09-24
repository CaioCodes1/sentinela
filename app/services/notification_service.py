"""Geração e despacho de notificações."""

from __future__ import annotations

import uuid
from datetime import date

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.entities import Contract, Notification
from app.domain.enums import AuditAction, AuditOutcome, NotificationStatus, NotificationType
from app.domain.exceptions import BusinessRuleError, NotFoundError
from app.domain.ports.notifier import NotificationGateway, NotificationRequest
from app.domain.ports.uow import UnitOfWork
from app.domain.rules import notification_dedupe_key
from app.services.audit_service import AuditService

logger = get_logger(__name__)


class NotificationService:
    def __init__(self, uow: UnitOfWork, gateway: NotificationGateway, settings: Settings) -> None:
        self._uow = uow
        self._gateway = gateway
        self._settings = settings
        self._audit = AuditService(uow)

    # -- Geração -----------------------------------------------------------
    def enqueue_expiration_alert(
        self, contract: Contract, *, days_left: int, recipient: str
    ) -> bool:
        """Enfileira o alerta de vencimento próximo. `False` se já existia.

        O marcador da chave de deduplicação é o **limiar** (`30`, `15`, `7`),
        não a data de hoje. Com a data, reprocessar um dia antigo geraria uma
        notificação nova para o mesmo limiar; com o limiar, cada contrato
        recebe no máximo um aviso de "faltam 30 dias" em toda a sua vida.
        """
        notification = Notification(
            contract_id=contract.id,
            notification_type=NotificationType.CONTRACT_EXPIRING,
            channel="email",
            recipient=recipient,
            subject=f"Contrato {contract.number} vence em {days_left} dia(s)",
            body=(
                f"O contrato {contract.number} encerra sua vigência em "
                f"{contract.end_date.strftime('%d/%m/%Y')}, daqui a {days_left} dia(s).\n"
                f"Mensalidade atual: R$ {contract.monthly_amount}.\n\n"
                "Acesse a plataforma para renovar ou registrar o encerramento."
            ),
            dedupe_key=notification_dedupe_key(
                contract_id=str(contract.id),
                notification_type=NotificationType.CONTRACT_EXPIRING,
                marker=f"d{days_left}",
            ),
        )
        return self._uow.notifications.add_if_absent(notification)

    def enqueue_expired_notice(self, contract: Contract, *, recipient: str) -> bool:
        notification = Notification(
            contract_id=contract.id,
            notification_type=NotificationType.CONTRACT_EXPIRED,
            channel="email",
            recipient=recipient,
            subject=f"Contrato {contract.number} venceu",
            body=(
                f"O contrato {contract.number} encerrou a vigência em "
                f"{contract.end_date.strftime('%d/%m/%Y')} e está marcado como vencido."
            ),
            dedupe_key=notification_dedupe_key(
                contract_id=str(contract.id),
                notification_type=NotificationType.CONTRACT_EXPIRED,
                # `end_date` e não "hoje": o vencimento de um contrato acontece
                # uma única vez, naquela data específica.
                marker=contract.end_date.isoformat(),
            ),
        )
        return self._uow.notifications.add_if_absent(notification)

    def enqueue_overdue_notice(
        self, contract: Contract, *, competence: str, due_date: date, recipient: str
    ) -> bool:
        notification = Notification(
            contract_id=contract.id,
            notification_type=NotificationType.INSTALLMENT_OVERDUE,
            channel="email",
            recipient=recipient,
            subject=f"Mensalidade {competence} do contrato {contract.number} em atraso",
            body=(
                f"A mensalidade da competência {competence}, com vencimento em "
                f"{due_date.strftime('%d/%m/%Y')}, consta em aberto."
            ),
            dedupe_key=notification_dedupe_key(
                contract_id=str(contract.id),
                notification_type=NotificationType.INSTALLMENT_OVERDUE,
                marker=competence,
            ),
        )
        return self._uow.notifications.add_if_absent(notification)

    # -- Despacho ----------------------------------------------------------
    def dispatch_pending(self, *, limit: int = 200) -> dict[str, int]:
        """Envia a fila e devolve o resumo.

        Uma falha de envio **não** interrompe o lote: a próxima notificação é
        tentada. Interromper faria um destinatário com e-mail inválido bloquear
        os avisos de todos os outros clientes daquela noite.

        **Um commit por notificação, e não um para o lote inteiro.**

        A primeira versão comitava uma vez no fim, com o argumento de que menos
        idas ao banco é melhor. Esse argumento está errado aqui, e o erro só
        apareceu com a pilha inteira de pé: cada envio faz uma chamada HTTP que
        pode levar segundos (timeout do provedor, espera entre tentativas), e
        com o commit no fim **a transação fica aberta e ociosa durante toda a
        rede**. Com 20 notificações e um provedor lento, passa de um minuto.

        O PostgreSQL derruba essa conexão: é para isso que serve o
        `idle_in_transaction_session_timeout` de 30 s configurado em
        `session.py`. O sintoma foi
        `server closed the connection unexpectedly` num INSERT de auditoria com
        51 linhas — uma mensagem que parece problema de rede ou de banco, e é
        problema de desenho.

        Manter transação aberta sobre E/S de rede é o defeito; o timeout foi o
        que o revelou, e por isso ele fica. Com o commit por item, cada
        transação dura o tempo de um UPDATE e um INSERT, e o progresso do lote
        fica gravado mesmo se o processo morrer no meio — que é melhor que
        perder tudo.
        """
        pending = self._uow.notifications.list_dispatchable(limit=limit)
        summary = {"processadas": 0, "enviadas": 0, "falhas": 0, "descartadas": 0}

        for notification in pending:
            summary["processadas"] += 1
            result = self._gateway.send(
                NotificationRequest(
                    channel=notification.channel,
                    recipient=notification.recipient,
                    subject=notification.subject,
                    body=notification.body,
                    # A chave de deduplicação é reaproveitada como chave de
                    # idempotência do provedor: se o retry reenviar algo que
                    # já tinha sido entregue, é ele que descarta.
                    idempotency_key=notification.dedupe_key,
                )
            )

            if result.delivered:
                notification.register_success(result.provider_message_id)
                summary["enviadas"] += 1
                self._audit.record(
                    action=AuditAction.NOTIFICATION_SENT,
                    resource_type="notification",
                    resource_id=notification.id,
                    details={
                        "tipo": notification.notification_type.value,
                        "tentativas": result.attempts,
                    },
                )
                self._uow.commit()
                continue

            notification.register_failure(
                result.error or "falha desconhecida",
                max_attempts=self._settings.notifier_max_attempts,
            )
            summary["falhas"] += 1
            if notification.status is NotificationStatus.DEAD_LETTER:
                summary["descartadas"] += 1

            self._audit.record(
                action=AuditAction.NOTIFICATION_FAILED,
                resource_type="notification",
                resource_id=notification.id,
                outcome=AuditOutcome.FAILURE,
                details={
                    "tipo": notification.notification_type.value,
                    "erro": notification.last_error,
                    "tentativas": notification.attempts,
                    "status": notification.status.value,
                },
            )
            self._uow.commit()

        if summary["processadas"]:
            logger.info("lote de notificações processado", extra=summary)
        return summary

    def retry(self, notification_id: uuid.UUID) -> Notification:
        """Recoloca na fila uma notificação descartada, por ação humana."""
        notification = self._uow.notifications.get(notification_id)
        if notification is None:
            raise NotFoundError("notificação não encontrada")
        if notification.status is NotificationStatus.SENT:
            raise BusinessRuleError("notificação já foi enviada")

        notification.status = NotificationStatus.PENDING
        # As tentativas anteriores são zeradas para que o contador de descarte
        # volte a valer. O histórico dessas tentativas não se perde: cada uma
        # gerou uma linha de auditoria.
        notification.attempts = 0
        notification.last_error = None

        self._audit.record(
            action=AuditAction.NOTIFICATION_FAILED,
            resource_type="notification",
            resource_id=notification.id,
            details={"acao": "reenfileirada_manualmente"},
        )
        self._uow.commit()
        return notification

    def search(
        self,
        *,
        contract_id: uuid.UUID | None = None,
        status: NotificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Notification], int]:
        return self._uow.notifications.search(
            contract_id=contract_id, status=status, limit=limit, offset=offset
        )
