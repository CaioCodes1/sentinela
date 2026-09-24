"""A automação diária.

Cinco passos, nesta ordem, e a ordem importa:

1. marcar como EXPIRING o que entrou na janela de alerta;
2. marcar como EXPIRED o que passou do término (renovando antes, se o contrato
   for de renovação automática);
3. marcar mensalidades vencidas e enfileirar a cobrança;
4. despachar a fila de notificações;
5. registrar a execução em `job_runs`.

O passo 4 vem depois dos três primeiros porque eles é que produzem o que ele
envia — invertido, o alerta gerado hoje só sairia amanhã.

Duas propriedades que o desenho garante:

**Idempotência.** Rodar duas vezes no mesmo dia não gera nada em dobro: cada
notificação tem chave única e cada parcela tem `(contrato, competência)` única.
Isso não é conveniência; é o que permite reprocessar um dia que falhou sem
telefonar para o cliente pedindo desculpa pelos e-mails repetidos.

**Exclusão mútua.** Uma trava consultiva no PostgreSQL impede que duas réplicas
rodem ao mesmo tempo. A segunda registra `SKIPPED` e sai — sem erro, porque não
é erro.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.core.config import Settings
from app.core.context import system_context, use_context
from app.core.logging import get_logger
from app.domain.entities import Contract, ContractEvent, JobRun
from app.domain.enums import AuditAction, AuditOutcome, ContractStatus, JobStatus
from app.domain.ports.notifier import NotificationGateway
from app.domain.ports.uow import UnitOfWork
from app.domain.rules import is_expired, matched_alert_threshold
from app.services.audit_service import AuditService
from app.services.contract_service import ContractService
from app.services.notification_service import NotificationService

logger = get_logger(__name__)

JOB_NAME = "verificacao-diaria-contratos"
LOCK_KEY = "sentinela:job:verificacao-diaria"


class AutomationService:
    def __init__(self, uow: UnitOfWork, gateway: NotificationGateway, settings: Settings) -> None:
        self._uow = uow
        self._settings = settings
        self._notifications = NotificationService(uow, gateway, settings)
        self._contracts = ContractService(uow)
        self._audit = AuditService(uow)

    def run_daily(self, *, reference: date | None = None) -> dict[str, Any]:
        today = reference or datetime.now(UTC).date()

        with use_context(system_context(JOB_NAME)):
            run = JobRun(job_name=JOB_NAME)
            self._uow.job_runs.add(run)

            if not self._uow.try_advisory_lock(LOCK_KEY):
                run.skip("outra instância já está executando o job")
                self._uow.commit()
                logger.info("execução ignorada: trava já tomada")
                return {"status": JobStatus.SKIPPED.value, "job_run_id": str(run.id)}

            self._audit.record(
                action=AuditAction.JOB_STARTED,
                resource_type="job",
                resource_id=run.id,
                details={"referencia": today.isoformat()},
            )
            self._uow.commit()

            try:
                stats = self._execute(today)
            except Exception as exc:
                # A transação já está suja pela exceção; é preciso limpá-la
                # antes de conseguir gravar qualquer coisa. Sem o rollback, o
                # PostgreSQL recusaria o próprio registro da falha com
                # "current transaction is aborted".
                self._uow.rollback()
                run = self._reattach_run(run)
                run.fail(str(exc))
                self._audit.record(
                    action=AuditAction.JOB_FAILED,
                    resource_type="job",
                    resource_id=run.id,
                    outcome=AuditOutcome.FAILURE,
                    details={"erro": type(exc).__name__},
                )
                self._uow.commit()
                logger.exception("automação diária falhou")
                raise

            run.finish(stats)
            self._audit.record(
                action=AuditAction.JOB_FINISHED,
                resource_type="job",
                resource_id=run.id,
                details=stats,
            )
            self._uow.commit()
            logger.info("automação diária concluída", extra=stats)
            return {"status": JobStatus.SUCCESS.value, "job_run_id": str(run.id), **stats}

    # -- Passos ------------------------------------------------------------
    def _execute(self, today: date) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "referencia": today.isoformat(),
            "contratos_analisados": 0,
            "alertas_gerados": 0,
            "contratos_marcados_a_vencer": 0,
            "contratos_expirados": 0,
            "renovacoes_automaticas": 0,
            "parcelas_atrasadas": 0,
            "cobrancas_geradas": 0,
        }

        contracts = self._uow.contracts.list_open_contracts()
        stats["contratos_analisados"] = len(contracts)
        alert_window = max(self._settings.expiration_alert_days)

        for contract in contracts:
            recipient = self._recipient_for(contract)

            # 1) alerta por limiar exato
            threshold = matched_alert_threshold(
                contract.end_date, today, self._settings.expiration_alert_days
            )
            if threshold is not None and recipient:
                if self._notifications.enqueue_expiration_alert(
                    contract, days_left=threshold, recipient=recipient
                ):
                    stats["alertas_gerados"] += 1

            # 2) transição de status
            if is_expired(contract.end_date, today):
                if contract.auto_renew:
                    stats["renovacoes_automaticas"] += self._auto_renew(contract)
                else:
                    contract.mark_expired()
                    stats["contratos_expirados"] += 1
                    self._uow.contract_events.add(
                        ContractEvent(
                            contract_id=contract.id,
                            event_type="EXPIRED",
                            payload={"em": today.isoformat()},
                        )
                    )
                    self._audit.record(
                        action=AuditAction.CONTRACT_EXPIRED,
                        resource_type="contract",
                        resource_id=contract.id,
                        details={"numero": contract.number},
                    )
                    if recipient:
                        self._notifications.enqueue_expired_notice(contract, recipient=recipient)
            elif (
                contract.status is ContractStatus.ACTIVE
                and (contract.end_date - today).days <= alert_window
            ):
                contract.mark_expiring()
                stats["contratos_marcados_a_vencer"] += 1

        # 3) mensalidades em atraso
        stats.update(self._process_overdue(today))

        # Confirma tudo o que os passos 1-3 produziram **antes** de despachar.
        # Sem este commit, uma falha de rede no envio desfaria também as
        # transições de status e as notificações geradas — e o job reprocessaria
        # tudo no dia seguinte como se nada tivesse acontecido.
        self._uow.commit()

        # 4) despacho
        stats["envio"] = self._notifications.dispatch_pending()
        return stats

    def _process_overdue(self, today: date) -> dict[str, int]:
        result = {"parcelas_atrasadas": 0, "cobrancas_geradas": 0}
        for installment in self._uow.installments.list_overdue(reference=today):
            was_pending = installment.status.value == "PENDING"
            installment.mark_overdue()
            if was_pending:
                result["parcelas_atrasadas"] += 1

            contract = self._uow.contracts.get(installment.contract_id)
            if contract is None or contract.status is ContractStatus.CANCELLED:
                # Contrato cancelado não gera cobrança nova, mesmo que a
                # parcela tenha ficado em aberto. Cobrar aqui é o defeito que
                # chega ao cliente como "cancelei e continuam me cobrando".
                continue

            recipient = self._recipient_for(contract)
            if recipient and self._notifications.enqueue_overdue_notice(
                contract,
                competence=installment.competence,
                due_date=installment.due_date,
                recipient=recipient,
            ):
                result["cobrancas_geradas"] += 1
        return result

    def _auto_renew(self, contract: Contract) -> int:
        """Renovação automática, tolerante a falha individual.

        Se a renovação de um contrato específico for recusada por regra de
        negócio (cliente desativado, por exemplo), o contrato é apenas marcado
        como vencido e o job segue. Deixar a exceção subir abortaria a execução
        inteira por causa de um registro.
        """
        try:
            successor = self._contracts.renew(contract.id)
        except Exception as exc:
            logger.warning(
                "renovação automática falhou; contrato marcado como vencido",
                extra={"contract_id": str(contract.id), "erro": type(exc).__name__},
            )
            contract.mark_expired()
            return 0
        logger.info(
            "contrato renovado automaticamente",
            extra={"origem": str(contract.id), "sucessor": str(successor.id)},
        )
        return 1

    def _recipient_for(self, contract: Contract) -> str | None:
        """E-mail de destino, vindo do cliente do contrato.

        Devolve `None` — em vez de levantar — quando não há e-mail utilizável.
        Um cadastro incompleto não pode derrubar a varredura dos outros 5.000
        contratos.
        """
        client = self._uow.clients.get(contract.client_id)
        if client is None:
            return None
        return client.email.value

    def _reattach_run(self, run: JobRun) -> JobRun:
        """Recupera o `JobRun` depois de um rollback.

        O rollback expulsa da sessão os objetos que ainda não tinham sido
        confirmados. Como o `JobRun` **foi** confirmado (commit logo após pegar
        a trava), ele existe no banco e pode ser recarregado. Se não estiver
        lá, cria-se outro — registrar a falha importa mais do que a elegância.
        """
        existing = next(
            (
                r
                for r in self._uow.job_runs.list_recent(job_name=JOB_NAME, limit=5)
                if r.id == run.id
            ),
            None,
        )
        if existing is not None:
            return existing
        fresh = JobRun(job_name=JOB_NAME)
        self._uow.job_runs.add(fresh)
        return fresh
