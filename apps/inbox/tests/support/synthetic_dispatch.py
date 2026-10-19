"""Executable dispatcher contract experiment, exclusively for disposable tests.

No provider, scheduler, endpoint or production adapter imports this module. All
receipts and freshness evidence are invented. Persisted statuses here belong to
synthetic fixtures, never real delivery. The production preflight still denies
dispatch. Do not reuse this harness as a live dispatcher or reconciliation API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from apps.inbox import reply_coordination as coordinator


class SyntheticGate(StrEnum):
    """An explicit script assumption, NOT evidence from account sync."""

    COMPLETE = "synthetic_complete"
    UNKNOWN = "synthetic_unknown"
    FAILED = "synthetic_failed"


class SyntheticOutcome(StrEnum):
    CONFIRMED = "synthetic_confirmed"
    DEFINITELY_NOT_SENT = "synthetic_definitely_not_sent"
    UNKNOWN = "synthetic_unknown"


class SyntheticFault(StrEnum):
    TIMEOUT = "synthetic_timeout"
    CRASH = "synthetic_crash"


class SyntheticCrashError(BaseException):
    """Simulated process loss: deliberately bypass ordinary exception handling."""


@dataclass
class ManualClock:
    instant: datetime

    def __call__(self) -> datetime:
        if timezone.is_naive(self.instant):
            raise ValueError("synthetic clock must be timezone aware")
        return self.instant

    def advance(self, seconds: int) -> None:
        self.instant += timedelta(seconds=seconds)


@dataclass(frozen=True)
class AttemptTicket:
    """Immutable local fence; no draft body and no provider authorization."""

    operation_id: UUID
    workspace_id: UUID
    account_id: UUID
    platform: str
    conversation_id: UUID
    actor_id: str
    claim_token: UUID
    fencing_token: int
    payload_fingerprint: str


@dataclass(frozen=True)
class SyntheticRequest:
    ticket: AttemptTicket
    target_id: UUID
    body: str


@dataclass(frozen=True)
class SyntheticReport:
    outcome: SyntheticOutcome
    code: str
    synthetic: bool = True
    send_allowed: bool = False


@dataclass(frozen=True)
class RecoverySnapshot:
    status: str
    outcome_code: str
    attempt_recorded: bool
    retry_allowed: bool = False
    send_allowed: bool = False


class ScriptedTransport:
    """In-memory fake only. A refusal guarantees NO synthetic remote effect.

    on_enter lets tests interleave real DB ingestion/pause with a fake request.
    It runs after the marker commits, with no harness transaction or row locks.
    The request list is an observation aid, never a dedupe/recovery mechanism.
    """

    def __init__(
        self,
        outcome: SyntheticOutcome | SyntheticFault,
        *,
        on_enter: Callable[[SyntheticRequest], None] | None = None,
    ) -> None:
        self.outcome = outcome
        self.on_enter = on_enter
        self.requests: list[SyntheticRequest] = []
        self.accepted_operation_ids: list[UUID] = []

    def exchange(self, request: SyntheticRequest) -> SyntheticOutcome:
        _require_autocommit()
        self.requests.append(request)
        if self.on_enter:
            self.on_enter(request)
        if self.outcome in {SyntheticOutcome.CONFIRMED, SyntheticFault.TIMEOUT, SyntheticFault.CRASH}:
            # Fault scripts deliberately model acceptance followed by response
            # loss. A restart cannot infer "not sent" from an exception.
            self.accepted_operation_ids.append(request.ticket.operation_id)
        if self.outcome is SyntheticFault.CRASH:
            raise SyntheticCrashError("synthetic loss after transport entry")
        if self.outcome is SyntheticFault.TIMEOUT:
            raise TimeoutError("synthetic lost response")
        if not isinstance(self.outcome, SyntheticOutcome):
            raise TypeError("only typed synthetic outcomes are accepted")
        return self.outcome


def _require_autocommit() -> None:
    # durable=True alone permits Django TestCase's special outer transaction.
    # Explicitly reject all nesting, including ATOMIC_REQUESTS/TestCase, because
    # a savepoint release is not a durable attempt boundary.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("synthetic dispatcher requires an outermost committed transaction")


def _require_gate(gate: SyntheticGate) -> None:
    if gate is not SyntheticGate.COMPLETE:
        raise coordinator.ReplyCoordinationError("synthetic_freshness_not_established")


class SyntheticDispatcher:
    """No automatic retry, provider reconciliation or network capability.

    Scope must be freshly authorized by the caller for EACH method. In
    particular, this harness cannot detect revoked principal grants encoded in
    a stale ReplyActorScope; persisted account/capture ownership is rechecked.
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    def begin_attempt(
        self,
        scope,
        *,
        operation_id: UUID,
        claim_token: UUID,
        fencing_token: int,
        gate: SyntheticGate = SyntheticGate.UNKNOWN,
    ) -> AttemptTicket:
        """Commit possible-effect evidence BEFORE even entering the fake.

        A crash inside this transaction rolls back the marker and leaves the
        local claim. A crash after commit is unknown even if transport has not
        actually run. That deliberate false uncertainty prevents blind retry.
        """
        _require_autocommit()
        _require_gate(gate)
        with transaction.atomic(durable=True):
            now = self.clock()
            preflight = coordinator.check_before_send(
                scope,
                operation_id=operation_id,
                claim_token=claim_token,
                fencing_token=fencing_token,
                now=now,
            )
            if preflight["send_allowed"] or preflight["live_dispatch_enabled"]:
                raise AssertionError("production preflight must remain disabled")
            operation = coordinator.mark_outcome_unknown(
                scope,
                operation_id=operation_id,
                claim_token=claim_token,
                fencing_token=fencing_token,
                now=now,
            )
            operation.outcome_code = "synthetic_pre_attempt"
            operation.save(update_fields=["outcome_code", "updated_at"])
            return AttemptTicket(
                operation.pk,
                operation.workspace_id,
                operation.social_account_id,
                operation.platform,
                operation.conversation_id,
                operation.actor_scope,
                operation.claim_token,
                operation.fencing_token,
                operation.payload_fingerprint,
            )

    def _load_ticket(self, scope, ticket: AttemptTicket):
        account, conversation, state, operation = coordinator._load_operation(scope, ticket.operation_id)
        # Every identity and fence must still match, including bool-as-int.
        if (
            not isinstance(ticket.fencing_token, int)
            or isinstance(ticket.fencing_token, bool)
            or (
                operation.workspace_id,
                operation.social_account_id,
                operation.platform,
                operation.conversation_id,
                operation.actor_scope,
                operation.claim_token,
                operation.fencing_token,
                operation.payload_fingerprint,
            )
            != (
                ticket.workspace_id,
                ticket.account_id,
                ticket.platform,
                ticket.conversation_id,
                ticket.actor_id,
                ticket.claim_token,
                ticket.fencing_token,
                ticket.payload_fingerprint,
            )
            or state.fencing_counter != ticket.fencing_token
        ):
            raise coordinator.ReplyCoordinationError("invalid_synthetic_ticket")
        coordinator._validate_payload(operation)
        return account, conversation, state, operation

    def _validate_current(self, account, conversation, state, operation, now):
        if (
            operation.status != "outcome_unknown"
            or operation.external_attempted_at is None
            or coordinator._active(state) != operation
        ):
            raise coordinator.ReplyCoordinationError("synthetic_attempt_not_active")
        if operation.lease_expires_at is None or now >= operation.lease_expires_at:
            raise coordinator.ReplyCoordinationError("lease_expired")
        coordinator._validate(
            account,
            conversation,
            state,
            expected_revision=operation.expected_revision,
            expected_generation=operation.expected_generation,
            target_id=operation.target_id,
            due=True,
            now=now,
        )

    def invoke(
        self,
        scope,
        ticket: AttemptTicket,
        transport: ScriptedTransport,
        *,
        gate: SyntheticGate = SyntheticGate.UNKNOWN,
    ) -> SyntheticReport:
        _require_autocommit()
        _require_gate(gate)
        if type(transport) is not ScriptedTransport:
            raise TypeError("this experiment accepts only the in-memory ScriptedTransport")
        with transaction.atomic(durable=True):
            account, conversation, state, operation = self._load_ticket(scope, ticket)
            self._validate_current(account, conversation, state, operation, self.clock())
            if operation.outcome_code != "synthetic_pre_attempt":
                raise coordinator.ReplyCoordinationError("synthetic_attempt_already_entered")
            operation.outcome_code = "synthetic_inflight"
            operation.save(update_fields=["outcome_code", "updated_at"])
            request = SyntheticRequest(ticket, operation.target_id, operation.body)
        # Locks must not span transport: pause and ingestion can now commit.
        # A native or local action AFTER this last check cannot recall a request.
        try:
            outcome = transport.exchange(request)
        except Exception:
            # Any unexpected/lost response is uncertain, never "known not sent".
            outcome = SyntheticOutcome.UNKNOWN
        return self.settle(scope, ticket, outcome)

    def settle(self, scope, ticket: AttemptTicket, outcome: SyntheticOutcome) -> SyntheticReport:
        """Apply a typed fake receipt only to its still-current synthetic work.

        Deliberately conservative: even a late fake refusal/acceptance does not
        clear a newer generation, pause, identity hold, or expired claim. There
        is no general provider reconciliation or force-clear operation here.
        """
        _require_autocommit()
        if not isinstance(outcome, SyntheticOutcome):
            raise TypeError("only typed synthetic receipts are accepted")
        try:
            with transaction.atomic(durable=True):
                account, conversation, state, operation = self._load_ticket(scope, ticket)
                self._validate_current(account, conversation, state, operation, self.clock())
                if operation.outcome_code != "synthetic_inflight":
                    return SyntheticReport(SyntheticOutcome.UNKNOWN, "synthetic_receipt_not_current")
                if outcome is SyntheticOutcome.UNKNOWN:
                    operation.outcome_code = outcome.value
                    operation.save(update_fields=["outcome_code", "updated_at"])
                else:
                    operation.status = "confirmed" if outcome is SyntheticOutcome.CONFIRMED else "failed"
                    operation.outcome_code = outcome.value
                    operation.save(update_fields=["status", "outcome_code", "updated_at"])
                    state.active_operation = None
                    # Only matching confirmation consumes this exact burst.
                    # A scripted no-effect refusal permits explicit new intent.
                    if outcome is SyntheticOutcome.CONFIRMED:
                        state.burst_started_at = None
                        state.due_at = None
                    state.save(update_fields=["active_operation", "burst_started_at", "due_at", "updated_at"])
                return SyntheticReport(outcome, outcome.value)
        except coordinator.ReplyCoordinationError:
            # Revocation may prevent even reloading the operation. The durable
            # pre-attempt evidence survives without bypassing account grants.
            return SyntheticReport(SyntheticOutcome.UNKNOWN, "synthetic_receipt_not_current")

    def recover(self, scope, *, operation_id: UUID) -> RecoverySnapshot:
        """Read persisted evidence; never reacquire, resend, or clear uncertainty."""
        _require_autocommit()
        with transaction.atomic(durable=True):
            _, _, _, operation = coordinator._load_operation(scope, operation_id)
            return RecoverySnapshot(
                operation.status,
                operation.outcome_code,
                operation.external_attempted_at is not None,
            )
