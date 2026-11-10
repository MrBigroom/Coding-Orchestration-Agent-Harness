"""Device-local metadata and change detection for Supabase synchronization.

Existing rows keep their integer IDs. Portable UUIDs live in ``sync_entities`` and cloud
payloads replace foreign keys with those UUIDs. No network credentials or device paths are
included in canonical payloads.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
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
}


def health(session: Session) -> dict[str, Any]:
    """Read the additive sync health record; a device that never ran reads as healthy."""
    stored = get_state(session, SYNC_HEALTH_KEY, {})
    values = dict(HEALTH_DEFAULTS)
    if isinstance(stored, dict):
        values.update(stored)
    return values


def write_health(session: Session, **changes: Any) -> dict[str, Any]:
    values = health(session)
    values.update(changes)
    set_state(session, SYNC_HEALTH_KEY, values)
    session.commit()
    return values


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
