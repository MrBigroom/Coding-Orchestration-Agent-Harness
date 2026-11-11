"""Device-local metadata and change detection for Supabase synchronization.

Existing rows keep their integer IDs. Portable UUIDs live in ``sync_entities`` and cloud
payloads replace foreign keys with those UUIDs. No network credentials or device paths are
included in canonical payloads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from orchestrator.core.models import (
    Agent,
    BuildLogEntry,
    HarnessAdapter,
    MemoryEntry,
    ModelProfile,
    Project,
    Provider,
    Review,
    Run,
    Schedule,
    Setting,
    SyncEntity,
    SyncState,
    Task,
    Verification,
    utcnow,
)

# These affect local safety or hardware and must not be changed by another device.
LOCAL_ONLY_SETTINGS = frozenset(
    {
        "allow_gui_commands",
        "allow_interactive_sessions",
        "allow_unattended_sessions",
        "max_parallel_runs",
        "max_parallel_runs_per_project",
        "scheduler_enabled",
        "worker_enabled",
        "supabase_sync_enabled",
        "supabase_sync_interval_seconds",
    }
)


@dataclass(frozen=True)
class EntitySpec:
    model: type[Any]
    entity_type: str
    # payload name -> (local foreign-key attribute, referenced entity type)
    refs: dict[str, tuple[str, str]] = field(default_factory=dict)
    local_only: frozenset[str] = frozenset()


SPECS = (
    EntitySpec(Project, "project", local_only=frozenset({"path"})),
    EntitySpec(Provider, "provider"),
    EntitySpec(ModelProfile, "model_profile", refs={"provider_ref": ("provider_id", "provider")}),
    EntitySpec(HarnessAdapter, "harness_adapter"),
    EntitySpec(Agent, "agent", refs={"project_ref": ("project_id", "project")}),
    EntitySpec(
        Task,
        "task",
        refs={
            "project_ref": ("project_id", "project"),
            "parent_task_ref": ("parent_task_id", "task"),
            "agent_ref": ("agent_id", "agent"),
        },
    ),
    EntitySpec(
        Run,
        "run",
        refs={"task_ref": ("task_id", "task")},
        local_only=frozenset({"artifacts_path", "worktree_path"}),
    ),
    EntitySpec(
        Review,
        "review",
        refs={"task_ref": ("task_id", "task"), "run_ref": ("run_id", "run")},
    ),
    EntitySpec(
        Verification,
        "verification",
        refs={"task_ref": ("task_id", "task"), "run_ref": ("run_id", "run")},
        local_only=frozenset({"evidence_path"}),
    ),
    EntitySpec(Schedule, "schedule", refs={"agent_ref": ("agent_id", "agent")}),
    EntitySpec(MemoryEntry, "memory_entry"),
    EntitySpec(
        BuildLogEntry,
        "build_log_entry",
        refs={"task_ref": ("task_id", "task"), "run_ref": ("run_id", "run")},
    ),
    EntitySpec(Setting, "setting"),
)
SPEC_BY_TYPE = {spec.entity_type: spec for spec in SPECS}


def _rows(session: Session, spec: EntitySpec) -> list[Any]:
    if spec.model is Setting:
        return list(session.exec(select(Setting).order_by(Setting.key)))  # type: ignore[union-attr]
    return list(session.exec(select(spec.model).order_by(spec.model.id)))


def local_id(row: Any) -> str:
    if isinstance(row, Setting):
        return row.key
    if row.id is None:
        raise ValueError("cannot synchronize a row without a local ID")
    return str(row.id)


def get_state(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(SyncState, key)
    return default if row is None else row.value_json


def set_state(session: Session, key: str, value: Any) -> None:
    row = session.get(SyncState, key) or SyncState(key=key)
    row.value_json = value
    row.updated_at = utcnow()
    session.add(row)


def device_id(session: Session) -> str:
    value = get_state(session, "device_id")
    if isinstance(value, str) and value:
        return value
    value = str(uuid4())
    set_state(session, "device_id", value)
    session.flush()
    return value


def get_mapping(session: Session, entity_type: str, row_id: str) -> SyncEntity | None:
    return session.exec(
        select(SyncEntity).where(
            SyncEntity.entity_type == entity_type,
            SyncEntity.local_id == row_id,
        )
    ).first()


def _reference_uuid(session: Session, entity_type: str, row_id: int | None) -> str | None:
    if row_id is None:
        return None
    mapping = get_mapping(session, entity_type, str(row_id))
    # Soft references can outlive their target (for example Task.agent_id).
    return mapping.uuid if mapping is not None else None


def canonical_payload(session: Session, spec: EntitySpec, row: Any) -> dict[str, Any]:
    ref_columns = {local for local, _ in spec.refs.values()}
    excluded = {"id", *spec.local_only, *ref_columns}
    payload = row.model_dump(mode="json", exclude=excluded)
    for name, (local, entity_type) in spec.refs.items():
        payload[name] = _reference_uuid(session, entity_type, getattr(row, local))
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def scan_local(session: Session) -> dict[str, Any]:
    """Assign stable UUIDs and mark new/changed rows dirty in one local transaction."""
    origin = device_id(session)
    now = utcnow()
    scanned = new = changed = 0
    for spec in SPECS:
        for row in _rows(session, spec):
            row_id = local_id(row)
            if spec.model is Setting and row_id in LOCAL_ONLY_SETTINGS:
                continue
            digest = payload_digest(canonical_payload(session, spec, row))
            mapping = get_mapping(session, spec.entity_type, row_id)
            if mapping is None:
                mapping = SyncEntity(
                    uuid=str(uuid4()),
                    entity_type=spec.entity_type,
                    local_id=row_id,
                    payload_hash=digest,
                    origin_device=origin,
                    modified_at=now,
                )
                session.add(mapping)
                session.flush()  # later rows can reference this UUID immediately
                new += 1
            elif mapping.payload_hash != digest:
                mapping.payload_hash = digest
                mapping.version = max(mapping.version, mapping.synced_version) + 1
                mapping.dirty = True
                mapping.origin_device = origin
                mapping.modified_at = now
                session.add(mapping)
                changed += 1
            scanned += 1
    session.commit()
    return {
        "device_id": origin,
        "scanned": scanned,
        "new": new,
        "changed": changed,
        "pending": len(pending(session)),
    }


def pending(session: Session) -> list[SyncEntity]:
    rows = session.exec(
        select(SyncEntity).where(SyncEntity.dirty == True).order_by(SyncEntity.modified_at)  # noqa: E712
    ).all()
    return [row for row in rows if row.conflict_json is None]


def mark_synced(session: Session, mapping: SyncEntity) -> None:
    mapping.synced_hash = mapping.payload_hash
    mapping.synced_version = mapping.version
    mapping.synced_at = utcnow()
    mapping.dirty = False
    mapping.conflict_json = None
    session.add(mapping)
    session.commit()


# Device-local health lives in the existing key/value sync_state table: no migration and no
# change to which entities sync. `sync_state` is deliberately never cloud-synchronized, so a
# stopped loop on one device cannot stop another. The key is additive data, not a new column.
SYNC_HEALTH_KEY = "sync_health"
HEALTH_DEFAULTS: dict[str, Any] = {
    "consecutive_failures": 0,
    "last_failure_at": None,
    "last_failure_kind": None,
    "last_failure_error": None,
    "stopped": False,
    "stopped_reason": None,
    "stopped_at": None,
    "next_retry_at": None,
    "backoff_seconds": 0.0,
    "log_occurrences": 0,
    "last_logged_error": None,
    "streak_notified": False,
    "stop_notified": False,
    "streak_notified_claim": None,
    "stop_notified_claim": None,
    "last_notify_error": None,
    "last_notify_failed_at": None,
    # The alert path gets the same treatment as the sync path: a distinct-signature dedupe and a
    # capped backoff, so a broken notifier cannot become the next unbounded ERROR source (T-41).
    "notify_failures": 0,
    "notify_backoff_seconds": 0.0,
    "notify_next_retry_at": None,
    "notify_log_occurrences": 0,
    "last_logged_notify_error": None,
}

# The notification claim's stale window has to cover the WHOLE emission, not just the moment of
# claiming. ``log_service.log_event`` commits the build-log row and the JSONL line first, then
# fans out to ``notification_service.notify_event`` in its own session: that delivery probes the
# daemon (2 s) and then runs the client (10 s), and a coalescing flush can add a little more. A
# window derived from the claim instant alone would let the next cycle re-claim and emit while
# the first emission is still in flight. This is a ceiling on an in-flight claim only; it never
# suppresses an alert whose emission actually failed, because a failure releases the claim.
HEALTH_FLAG_CLAIM_LEASE_SECONDS = 120.0


class HealthConflict(RuntimeError):
    """The health record changed under us on every compare-and-set attempt."""


def _read_health_raw(session: Session) -> tuple[bool, str | None, dict[str, Any]]:
    """Read the stored JSON verbatim, bypassing the ORM identity map.

    The raw text is the compare-and-set token: it is what the conditional ``UPDATE`` matches on,
    so a write from another session or process (the desktop loop vs ``orchestrator sync now``)
    cannot be silently overwritten by a value read before that write landed.
    """
    row = session.connection().exec_driver_sql(
        "SELECT value_json FROM sync_state WHERE key = ?", (SYNC_HEALTH_KEY,)
    ).first()
    if row is None:
        return False, None, {}
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    return True, raw, stored if isinstance(stored, dict) else {}


def health(session: Session) -> dict[str, Any]:
    """Read the additive sync health record; a device that never ran reads as healthy."""
    _exists, _raw, stored = _read_health_raw(session)
    values = dict(HEALTH_DEFAULTS)
    values.update(stored)
    return values


def update_health(
    session: Session,
    mutate: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    attempts: int = 8,
) -> dict[str, Any]:
    """Compare-and-set read-modify-write of the health record.

    There is deliberately no single-writer assumption. The desktop background loop and a CLI
    ``sync now`` are separate processes sharing one SQLite file, so two of them can read the same
    streak (or the same ``*_notified`` flag) before either writes. Every write is therefore a
    conditional ``UPDATE ... WHERE value_json = <the exact text we read>``; if another writer got
    there first the update matches zero rows, and this function re-reads and re-applies ``mutate``
    to the new value. Increments are re-derived on each attempt, so a lost update is impossible,
    and a claim that returns ``None`` on an already-set flag is won by exactly one caller.

    ``mutate`` receives a complete health dict and returns the fields to change, or ``None`` to
    abandon the write (a claim that lost the race). The row is created with an ``INSERT`` when a
    device has never recorded health; a racing insert is retried as a normal CAS conflict.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    for _ in range(attempts):
        exists, raw, stored = _read_health_raw(session)
        values = dict(HEALTH_DEFAULTS)
        values.update(stored)
        changes = mutate(dict(values))
        if changes is None:
            return values
        values.update(changes)
        # Canonical text: the next CAS compares against exactly these bytes.
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
        now = utcnow()
        result = None
        try:
            if exists:
                result = session.connection().exec_driver_sql(
                    "UPDATE sync_state SET value_json = ?, updated_at = ? "
                    "WHERE key = ? AND value_json IS ?",
                    (payload, now, SYNC_HEALTH_KEY, raw),
                )
            else:
                session.connection().exec_driver_sql(
                    "INSERT INTO sync_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    (SYNC_HEALTH_KEY, payload, now),
                )
        except (IntegrityError, OperationalError):
            # A racing insert (unique key) or a WAL snapshot taken before another process
            # committed is not a lost write: roll back and re-read, then CAS again.
            session.rollback()
            continue
        session.commit()
        if result is None or result.rowcount == 1:
            # The raw write bypassed the ORM; make any cached SyncState row re-read next time.
            session.expire_all()
            return values
        session.rollback()
    raise HealthConflict(
        f"sync health changed on {attempts} consecutive compare-and-set attempts"
    )


def write_health(session: Session, **changes: Any) -> dict[str, Any]:
    """Set absolute health fields through the same compare-and-set path as the counters."""
    return update_health(session, lambda _current: changes)


def _live_claim(current: dict[str, Any], flag: str, now: datetime) -> bool:
    """Is another emitter's unexpired claim recorded for ``flag``?

    An unparseable lease is treated as expired: wedging the alert forever because of a corrupt
    timestamp is worse than one possible duplicate event.
    """
    claim = current.get(f"{flag}_claim")
    if not isinstance(claim, dict) or not claim.get("token"):
        return False
    try:
        deadline = datetime.fromisoformat(str(claim.get("expires_at")))
    except (TypeError, ValueError):
        return False
    return now < deadline


def claim_health_flag(session: Session, flag: str, **extra: Any) -> str | None:
    """Lease the right to emit ``flag``'s notification; returns a per-attempt token or ``None``.

    This is the notification once-per-streak gate and it has the three parts a cross-process
    claim needs: (1) the lease window (``HEALTH_FLAG_CLAIM_LEASE_SECONDS``) covers the whole
    emission, so a slow delivery is not re-claimed by the next cycle; (2) the token is unique
    per attempt, so two claims are distinguishable even inside the same second; (3) the flag
    itself is NOT set here -- ``complete_health_flag`` writes it with a compare-and-set on this
    token after the emission actually succeeded. A caller that loses the race gets ``None`` and
    must write nothing and log nothing.
    """
    token = uuid4().hex
    won: dict[str, str | None] = {"token": None}

    def mutate(current: dict[str, Any]) -> dict[str, Any] | None:
        # Re-evaluated on every CAS attempt: a failed attempt must not leave this set, or the
        # loser of the race would report a claim it never wrote.
        won["token"] = None
        now = utcnow()
        if current.get(flag) or _live_claim(current, flag, now):
            return None
        won["token"] = token
        return {
            f"{flag}_claim": {
                "token": token,
                "claimed_at": now.isoformat(),
                "expires_at": (
                    now + timedelta(seconds=HEALTH_FLAG_CLAIM_LEASE_SECONDS)
                ).isoformat(),
            },
            **extra,
        }

    update_health(session, mutate)
    return won["token"]


def _settle_health_flag(
    session: Session, flag: str, token: str, changes: dict[str, Any]
) -> bool:
    """Apply ``changes`` only while ``token`` still owns the claim; ``True`` iff it landed.

    The compare-and-set is on the claim token, not on a flag read earlier, so a settlement can
    never overwrite a newer attempt's claim (the claim expired and another process took over).
    """
    settled = {"value": False}

    def mutate(current: dict[str, Any]) -> dict[str, Any] | None:
        settled["value"] = False
        lease = current.get(f"{flag}_claim")
        if not isinstance(lease, dict) or lease.get("token") != token:
            return None
        settled["value"] = True
        return changes

    update_health(session, mutate)
    return settled["value"]


def complete_health_flag(session: Session, flag: str, token: str, **extra: Any) -> bool:
    """Record a delivered emission: set ``flag`` and clear the claim in one CAS write.

    This is the terminal write the earlier shape was missing. Only the owner of ``token`` lands
    it, and only after the emission returned; an interrupted emission therefore leaves the flag
    false (the claim is released or expires) instead of recording an alert nobody saw.
    """
    return _settle_health_flag(
        session,
        flag,
        token,
        {
            flag: True,
            f"{flag}_claim": None,
            # A delivered alert supersedes any earlier emission failure for this streak: the
            # reason, the retry schedule and the log-dedup counter all reset, so the next streak
            # starts from a clean slate and logs its first failure again.
            "last_notify_error": None,
            "last_notify_failed_at": None,
            "notify_failures": 0,
            "notify_backoff_seconds": 0.0,
            "notify_next_retry_at": None,
            "notify_log_occurrences": 0,
            "last_logged_notify_error": None,
            **extra,
        },
    )


def release_health_flag(
    session: Session, flag: str, token: str, *, error: str | None = None
) -> bool:
    """Give up a claim after a failed emission so the next cycle can notify again.

    The flag stays false: a failed emission is not a delivered alert. The failure itself is
    recorded separately in ``last_notify_error`` / ``last_notify_failed_at`` so the reason an
    alert was not seen survives without changing the frozen notification status vocabulary.
    The retry schedule and its dedupe counter are written by ``SyncRunner._record_notify_failure``
    in the same cycle: releasing the claim is what makes a retry possible, the schedule is what
    keeps it bounded.
    """
    changes: dict[str, Any] = {f"{flag}_claim": None}
    if error is not None:
        changes["last_notify_error"] = str(error)[:500]
        changes["last_notify_failed_at"] = utcnow().isoformat()
    return _settle_health_flag(session, flag, token, changes)


def clear_health(session: Session) -> dict[str, Any]:
    """A successful cycle clears the streak, the stop, and the dedupe counter together."""
    return write_health(session, **HEALTH_DEFAULTS)


def reinitialise(
    session: Session, remote_versions: dict[str, int], *, dry_run: bool = True
) -> dict[str, Any]:
    """Re-baseline local version counters to remote truth; never edits synchronized payloads.

    The remote version is a per-entity compare-and-swap counter. A local mapping is only allowed
    to present ``synced_version + 1`` on its next push. When the two sides disagree (restored
    database, a lost push, the legacy `0 -> 3` drift), this aligns ``synced_version`` with the
    version the remote actually holds and leaves a dirty record's payload pending. Rows with an
    unresolved payload conflict are reported and left untouched: silently discarding one side is
    not a version repair. A dirty row whose remote version is AHEAD of the local synced version
    is also left untouched (``remote_ahead_skipped``): re-baselining forward would let the next
    push overwrite remote changes this device has not seen. The stop is only cleared when every
    row could be aligned. With ``dry_run`` the plan is returned and no row or health key is
    written, so an operator can inspect it first.
    """
    changes: list[dict[str, Any]] = []
    conflicts_skipped = 0
    remote_ahead_skipped = 0
    for mapping in session.exec(select(SyncEntity)):
        if mapping.conflict_json is not None:
            conflicts_skipped += 1
            continue
        synced = int(remote_versions.get(mapping.uuid, 0))
        if mapping.dirty and synced > mapping.synced_version:
            # The remote has changes this device has never seen. Re-baselining forward would let
            # the next push overwrite them, defeating the compare-and-swap guarantee, so this
            # row is reported until the content conflict is resolved by a human.
            remote_ahead_skipped += 1
            continue
        version = synced + 1 if mapping.dirty else synced
        if mapping.synced_version == synced and mapping.version == version:
            continue
        changes.append(
            {
                "entity_type": mapping.entity_type,
                "entity_id": mapping.uuid,
                "local_id": mapping.local_id,
                "synced_version": [mapping.synced_version, synced],
                "version": [mapping.version, version],
            }
        )
        if not dry_run:
            mapping.synced_version = synced
            mapping.version = version
            session.add(mapping)
    # A skipped row means the stop is real: resuming the loop would only re-hit a conflict.
    health_cleared = not dry_run and not conflicts_skipped and not remote_ahead_skipped
    if not dry_run:
        session.commit()
        if health_cleared:
            clear_health(session)
    return {
        "dry_run": dry_run,
        "changes": changes,
        "change_count": len(changes),
        "conflicts_skipped": conflicts_skipped,
        "remote_ahead_skipped": remote_ahead_skipped,
        "health_cleared": health_cleared,
    }


def status(session: Session) -> dict[str, int]:
    rows = list(session.exec(select(SyncEntity)))
    return {
        "tracked": len(rows),
        "pending": sum(row.dirty and row.conflict_json is None for row in rows),
        "conflicts": sum(row.conflict_json is not None for row in rows),
        "synced": sum(not row.dirty and row.conflict_json is None for row in rows),
    }


# Natural identities let a fresh installation adopt matching seeded rows instead of
# duplicating providers, adapters, and default memory under a second UUID.
NATURAL_KEYS = {
    "project": ("slug",),
    "provider": ("name",),
    "model_profile": ("name",),
    "harness_adapter": ("name",),
    "agent": ("name",),
    "task": ("short_id",),
    "run": ("short_id",),
    "schedule": ("name",),
    "memory_entry": ("category", "scope", "project_slug", "role", "content"),
    "setting": ("key",),
}


class DeferredRemote(Exception):
    """A remote row references another row that has not been applied yet."""


def local_row(session: Session, spec: EntitySpec, row_id: str) -> Any | None:
    if spec.model is Setting:
        return session.get(Setting, row_id)
    return session.get(spec.model, int(row_id))


def _natural_match(session: Session, spec: EntitySpec, payload: dict[str, Any]) -> Any | None:
    keys = NATURAL_KEYS.get(spec.entity_type)
    if not keys:
        return None
    return next(
        (
            row
            for row in _rows(session, spec)
            if all(getattr(row, key) == payload.get(key) for key in keys)
        ),
        None,
    )


def _set_conflict(session: Session, mapping: SyncEntity, record: dict[str, Any]) -> None:
    mapping.conflict_json = record
    mapping.dirty = True
    session.add(mapping)
    session.commit()


def _finish_remote(
    session: Session,
    mapping: SyncEntity,
    row: Any,
    record: dict[str, Any],
) -> None:
    spec = SPEC_BY_TYPE[record["entity_type"]]
    digest = payload_digest(canonical_payload(session, spec, row))
    mapping.payload_hash = digest
    mapping.synced_hash = digest
    mapping.version = int(record["version"])
    mapping.synced_version = int(record["version"])
    mapping.dirty = False
    mapping.origin_device = str(record.get("origin_device") or mapping.origin_device)
    mapping.synced_at = utcnow()
    mapping.conflict_json = None
    session.add(mapping)
    session.commit()


def _resolved_payload(
    session: Session, spec: EntitySpec, payload: dict[str, Any]
) -> dict[str, Any]:
    data = {key: value for key, value in payload.items() if key not in spec.refs}
    for name, (local, entity_type) in spec.refs.items():
        reference = payload.get(name)
        if reference is None:
            data[local] = None
            continue
        mapping = session.get(SyncEntity, str(reference))
        if mapping is None or mapping.entity_type != entity_type:
            raise DeferredRemote(f"missing {entity_type} reference {reference}")
        data[local] = int(mapping.local_id)
    return data


def _write_remote_row(
    session: Session,
    spec: EntitySpec,
    mapping: SyncEntity | None,
    entity_id: str,
    payload: dict[str, Any],
) -> tuple[Any, SyncEntity]:
    data = _resolved_payload(session, spec, payload)
    row = local_row(session, spec, mapping.local_id) if mapping is not None else None
    if row is None:
        if spec.model is Project:
            data.setdefault("path", "")  # must be replaced with this device's checkout path
        row = spec.model.model_validate(data)
        session.add(row)
        session.flush()
    else:
        candidate = spec.model.model_validate({**row.model_dump(), **data})
        for key in data:
            setattr(row, key, getattr(candidate, key))
        session.add(row)
        session.flush()
    if mapping is None:
        mapping = SyncEntity(
            uuid=entity_id,
            entity_type=spec.entity_type,
            local_id=local_id(row),
            payload_hash="",
            origin_device="",
        )
        session.add(mapping)
        session.flush()
    return row, mapping


def _apply_remote_record(session: Session, record: dict[str, Any]) -> str:
    entity_type = str(record["entity_type"])
    spec = SPEC_BY_TYPE[entity_type]
    entity_id = str(record["entity_id"])
    payload = record["payload"]
    if not isinstance(payload, dict):
        raise ValueError("remote payload must be an object")
    remote_version = int(record["version"])
    remote_digest = payload_digest(payload)
    mapping = session.get(SyncEntity, entity_id)

    if mapping is None:
        natural = _natural_match(session, spec, payload)
        if natural is not None:
            existing = get_mapping(session, entity_type, local_id(natural))
            if existing is not None:
                if existing.payload_hash != remote_digest:
                    _set_conflict(session, existing, record)
                    return "conflicts"
                existing.uuid = entity_id
                session.add(existing)
                session.flush()
                _finish_remote(session, existing, natural, record)
                return "adopted"
        row, mapping = _write_remote_row(session, spec, None, entity_id, payload)
        _finish_remote(session, mapping, row, record)
        return "applied"

    if remote_version <= mapping.synced_version:
        return "skipped"
    if mapping.dirty:
        if mapping.payload_hash == remote_digest:
            row = local_row(session, spec, mapping.local_id)
            _finish_remote(session, mapping, row, record)
            return "adopted"
        _set_conflict(session, mapping, record)
        return "conflicts"
    row = local_row(session, spec, mapping.local_id)
    row, mapping = _write_remote_row(session, spec, mapping, entity_id, payload)
    _finish_remote(session, mapping, row, record)
    return "applied"


def apply_remote_records(session: Session, records: list[dict[str, Any]]) -> dict[str, int]:
    """Apply remote rows in dependency order, retrying unresolved forward references."""
    order = {spec.entity_type: index for index, spec in enumerate(SPECS)}
    remaining = sorted(
        (record for record in records if record.get("entity_type") in SPEC_BY_TYPE),
        key=lambda record: order[record["entity_type"]],
    )
    result = {"applied": 0, "adopted": 0, "skipped": 0, "conflicts": 0, "errors": 0}
    result["errors"] = sum(record.get("entity_type") not in SPEC_BY_TYPE for record in records)
    while remaining:
        deferred = []
        progressed = False
        for record in remaining:
            try:
                outcome = _apply_remote_record(session, record)
            except DeferredRemote:
                session.rollback()
                deferred.append(record)
                continue
            except (TypeError, ValueError):
                session.rollback()
                result["errors"] += 1
                continue
            result[outcome] += 1
            progressed = True
        if not deferred:
            break
        if not progressed:
            result["errors"] += len(deferred)
            break
        remaining = deferred
    return result
