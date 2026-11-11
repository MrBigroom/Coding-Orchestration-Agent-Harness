# Supabase cloud synchronization

Coding Orchestrator remains **local-first**. SQLite is the active runtime database; Supabase
is a synchronization layer, not a requirement for running the application. Devices can work
offline and reconcile when connectivity returns.

## Scope

The synchronization envelope covers projects, tasks, agents, runs, reviews, verifications,
schedules, providers, model profiles, harness adapters, memory entries, build-log entries,
and portable settings. Tasks and run history are included because logs reference them.

The following stay on each device:

- API keys, Supabase credentials, access tokens, and `.env` values
- Project checkout paths, worktree paths, artifact paths, and evidence paths
- Worker/scheduler enablement, parallelism limits, and permission to run GUI commands, Chat
  sessions or standalone runs
- Chat sessions, standalone run records, and agent output folders
- Automatic-sync enablement and cadence, which are configured independently on each device
- Git repositories, worktrees, run artifacts, and SQLite WAL files

Schedules sync, but each device with its scheduler on runs them itself. To avoid duplicate
runs, leave the scheduler on for one device only. This matters most for one-time schedules: a
device that was off at the chosen time runs a missed schedule at startup, which can happen
before it pulls the news that another device already ran it.

Memory Markdown is regenerated locally. JSON database records, not rendered Markdown, are
the memory synchronization source.

## Identity and change detection

Local integer primary keys remain unchanged. A local `sync_entities` table maps every
record to a permanent UUID and tracks its payload hash, local version, last synchronized
version, and conflict state. Reference fields are serialized as UUID references rather than
device-local integer IDs.

A background scanner detects inserts and edits by comparing canonical JSON payload hashes.
New or changed records increment their local version and become pending. This avoids
requiring every service to implement its own change hook.

## Version protocol and rejected transitions (T-41)

The version counter is **remote per-entity state**, owned by the cloud row, not a device-global
cursor:

- `orchestrator_sync_records.version` is the compare-and-swap counter stored in Supabase. The
  cloud push function (`supabase/migrations/0001_orchestrator_sync.sql:42` `orchestrator_sync_push`)
  requires `p_version = p_expected_version + 1`; lines 62-63 are the exact rejection:

  ```sql
  if p_expected_version < 0 or p_version != p_expected_version + 1 then
      raise exception 'invalid version transition: % -> %', p_expected_version, p_version;
  ```

  `p_expected_version` is the last remote version this device observed (`0` when the row is new).
  If the row exists but its version differs from `p_expected_version`, the guarded `UPDATE` at
  lines 80-90 matches nothing and the function returns `{"status": "conflict"}` instead of
  overwriting. A device can therefore never overwrite a remote edit it has not seen.
- Locally, `sync_entities` carries both numbers: `synced_version` is the last remote version this
  device adopted or successfully wrote, and `version` is the local dirty revision. They are
  different columns on purpose.
- `sync_state` has **no version row by design**. It is device-local bookkeeping (`device_id`, and
  since T-41 the additive `sync_health` key); versions are per entity in `sync_entities`. A
  device showing only `device_id` in `sync_state` while `sync_entities` has thousands of rows is
  therefore normal, not a missing cursor.

A rejection reading `invalid version transition: 0 -> 3` means the local side presented local
revision `3` as the next **remote** version while the remote still expected `0 -> 1`. That is what
the pre-T-41 push did: it sent `mapping.version` (which increments on every local payload change)
instead of `mapping.synced_version + 1`. A record edited three times before its first successful
push therefore produced a permanent 400 on every cycle.

The live evidence was re-measured read-only on 2026-11-11 and reconciled against the operator's
earlier figure. The apparent disagreement was a counting-pattern difference plus a growing file,
not a contradiction:

| measurement | log prefix | `grep -c 'invalid version transition'` (all pairs) | `grep -c '... 0 -> 3'` |
| --- | --- | --- | --- |
| operator, task filed | 275,740 lines | **7,373** | 6,985 |
| T-41 docs (earlier round) | 277,088 lines | 7,416 | 7,028 |
| bundle snapshot | see `verify-evidence/T-41/live_measurement.txt` | recorded there | recorded there |

The operator's 7,373 is the **all-transitions** count at the 275,740-line prefix; the 7,028 quoted
earlier was the **exactly `0 -> 3`** count at a later prefix. The 388 other transition lines in the
7,416 total are the same defect at other revisions: 370 `9 -> 14`, 14 `9 -> 11`, 2 `9 -> 12`, and
2 `0 -> 2` (counts at the 275,740-line prefix are stable and reproducible because the log is
append-only). `background Supabase synchronization failed` is a slightly larger count than the
transitions because a handful of historical cycles failed for a different reason (mostly a
pre-fix `AttributeError: 'ModelProfile' object has no attribute 'context_window'`, plus isolated
network/HTTP-522 errors); they are the same missing-visibility defect, not version rejections and
the exact breakdown is in `live_measurement.txt`. At
the measured DB snapshot `sync_entities` held 1,459 rows (1,426 when the task was filed; 911 still
dirty) and `sync_state` held exactly one key, `device_id` (written 2026-10-28), with no version row.
The raw commands and output behind every number in this section are committed in
`verify-evidence/T-41/live_measurement.txt`; the reference commands are `wc -l`, `head -n`, `grep
-c`/`grep -oE`, and a `mode=ro` SQLite query.

Retrying could never succeed because nothing on either side changed: the remote version stayed 3
while the local push kept presenting the local revision 3 as the next remote version.

After the fix, the transport sends `p_expected_version = synced_version` and
`p_version = synced_version + 1`, then records the version that actually landed as both `version`
and `synced_version`. The local revision counter still tracks local edits; it no longer travels to
the cloud.

### Re-initialise after a version rejection or a restored database

`orchestrator sync reinit` re-baselines local counters to the versions the remote actually holds.
It downloads versions only (never payloads), leaves dirty payloads pending so they upload on the
next cycle, and prints a plan by default:

```bash
orchestrator sync reinit           # inspect the planned counter changes; writes nothing (dry run is the default)
orchestrator sync reinit --apply   # write them and clear the stopped/failure streak
orchestrator sync now              # normal cycle; a success clears the stop
```

Records with an unresolved payload conflict are reported as `conflicts_skipped` and never
touched: a version repair must not silently choose one side of a real content conflict. A local
row that is dirty while the remote version is **ahead** of its `synced_version` is reported as
`remote_ahead_skipped` and also left untouched: re-baselining forward would let the next push
overwrite remote changes this device has not seen. The stopped/failure streak is only cleared
when every row could be aligned. If the remote is unreachable, `reinit` fails rather than
guessing, because the remote version is the authority it is aligning to.

### Failure handling and visibility

A failed cycle is classified rather than retried blindly:

- **Permanent** (4xx other than 408/425/429, including the version-transition 400, bad credentials,
  and a malformed response) stops automatic attempts immediately and records a stopped state.
- **Transient** (5xx, 408/425/429, timeout, network) retries indefinitely with exponential backoff.
  The cap is on the **delay**, never on the number of attempts: a laptop that is offline for an
  hour must recover by itself, and must not need the same repair (`reinit`) as a permanent 400.
  The first retry is never sooner than the configured interval, so with the default 60 s interval
  the sequence is 60, 120, 240, 480, 900, 900, ... seconds. The 900 s ceiling is reachable at the
  fifth retry; when the interval itself is longer than 900 s the ceiling is raised to the interval
  so a backoff can never poll more often than the configured cadence.
- A success clears the streak, the stop, and the log-dedup counter.

A transient failure therefore never sets `stopped`; it sets `consecutive_failures`,
`next_retry_at`, and `backoff_seconds`. Only a permanent client error sets `stopped` and
`stopped_reason`.

The stopped reason and consecutive-failure count are exposed by `orchestrator sync status`, the
`GET /api/sync/status` response, and **Settings → Cloud Sync** in both the React and fallback UIs.
The health record lives in the device-local `sync_state` key `sync_health` (additive data, not a
schema change). The desktop loop and a CLI `sync now` are separate processes sharing one SQLite
file, so **no single-writer assumption is made**: every health write is a compare-and-set
(`sync_service.update_health`) that re-reads and re-applies the change if another process wrote
first. The once-per-streak alert uses the same conditional write in three parts
(`claim_health_flag` / `complete_health_flag` / `release_health_flag`): an emission is leased with
a per-attempt token whose window covers the whole notification delivery path, the
`*_notified` flag is written **only after** the emission returned (a compare-and-set on that
token), and a failed emission releases the claim and records `last_notify_error` so the next
cycle tries again. An emission that fails therefore never leaves a record claiming the operator
was told, and two racing processes produce one event, not two.

A `sync_failed` build-log event (and therefore one notification decision, subject to the master
switch and the `notification_sync_failed` subscription) is emitted at most twice per failure
streak: once when a transient streak becomes persistent (3 consecutive failures), and once when
the loop stops on a permanent error. It is never emitted per retry, and it is emitted whether the
failing cycle came from the background loop or a manual `sync now`. While stopped, the background
coroutine keeps polling preferences but makes no further attempts: the explicit recovery paths are
`sync reinit --apply` and a manual `sync now` that succeeds.

**Log deduplication rule:** a failure line is keyed by the exact `<exception type>: <message>`
signature. The first occurrence logs once, with a traceback; identical repeats only increment
`sync_health.log_occurrences` and are logged at DEBUG, and a different signature logs again and
restarts the count. This is what keeps a permanent 400 from writing thousands of tracebacks
(6,682 exact `0 -> 3` lines at filing, 6,985 in the 275,740-line reconciliation window above). A
signature's counter lives in `sync_health.log_occurrences`, so `orchestrator sync status` still
reports the true repeat count without another traceback.

The **alert path is held to the same rule**, because the code that removes an unbounded log must
not create one. If `log_service.log_event` itself fails (the notification fan-out is broken), the
failure is keyed by the same `<exception type>: <message>` signature, its first occurrence logs
once and identical repeats only increment `sync_health.notify_log_occurrences`, and the retry is
delayed by the same capped exponential backoff (persisted as `sync_health.notify_next_retry_at`),
not re-attempted every interval forever. The claim is still released first, so a repaired notifier
alerts on its next attempt. `orchestrator sync status`, `GET /api/sync/status` and both Settings
UIs therefore expose `last_notify_error`, `last_notify_failed_at`, `notify_failures` and
`notify_next_retry_at`: a failed alert is visible where the operator looks, not only in a log. A
delivered alert resets all four.

## Conflict rules

Cloud pushes use an atomic compare-and-swap function with the last remotely observed
version. A device cannot overwrite a remote change it has not seen.

- Append-only logs and run history normally merge without conflicts.
- Simultaneous edits to the same record produce an explicit conflict.
- Portable settings use the same version checks; no silent cloud overwrite is allowed.
- Deletion propagation is intentionally deferred until tombstones and dependency-safe
  deletion behavior are implemented. The initial schema and local foundation are upsert-only.

## Cloud schema

`supabase/migrations/0001_orchestrator_sync.sql` creates one authenticated-user-owned
envelope table and an atomic push function. Row Level Security is enabled, and only the
`authenticated` role receives access. The application must use a user session, never a
service-role key.

Apply the migration only to a dedicated Supabase project. Do not apply it to an unrelated
existing project. The initial migration has been applied to the dedicated
`Coding-Orchestrator` project (`kbfgjbzxbcrxpyxlhdby`).

## Device setup

1. In Supabase Authentication, create the email/password user that will own the synchronized
   records. Use a dedicated, strong password.
2. Copy the project's publishable API key from Settings → API Keys. Do not use a service-role
   or secret key.
3. Set these values in the device's `.env`:

   ```bash
   ORCHESTRATOR_SUPABASE_URL=https://kbfgjbzxbcrxpyxlhdby.supabase.co
   ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY=
   ORCHESTRATOR_SUPABASE_EMAIL=
   ORCHESTRATOR_SUPABASE_PASSWORD=
   ORCHESTRATOR_SUPABASE_SYNC_BATCH_SIZE=100
   ORCHESTRATOR_SUPABASE_SYNC_ENABLED=false
   ORCHESTRATOR_SUPABASE_SYNC_INTERVAL_SECONDS=60
   ```

4. Run `orchestrator sync status` to inspect local change tracking.
5. Run `orchestrator sync now`, or use **Settings → Cloud Sync → Sync now**, to authenticate,
   download remote records, and upload pending local records. The first run can upload
   application data; review the scope above first. Manual runs work while automatic mode is off.
6. To enable automatic synchronization, turn on **Automatic background sync** in Settings and
   choose an interval from 15 seconds to 24 hours. It is disabled by default, and the enablement
   and cadence remain device-local. The running server notices changes without a restart.

The password is used only by the local Supabase Auth client. Access and refresh tokens stay
in memory. Project paths must be filled in after a project arrives on a new device.

## Second-device setup

Use this checklist to import the synchronized records into another installation without
copying the original device's SQLite database.

1. **Install and initialize Coding Orchestrator normally.** Startup migrations create the local
   sync tables. Keep automatic synchronization disabled during the initial import.
2. **Transfer only the Supabase connection settings securely.** Copy the same Supabase URL,
   publishable key, sync-user email, and sync-user password into the new device's `.env` using
   a password manager or another encrypted channel. Do not send them through chat, commit them,
   or copy the entire original `.env`. Never substitute a service-role or secret API key.
3. **Set file permissions and restart.** On Linux/macOS, run `chmod 600 .env`, then restart the
   backend so it loads the new credentials. The Settings page should report **configured** while
   automatic sync remains off.
4. **Inspect and run the first import.** Run `orchestrator sync status`, followed by
   `orchestrator sync now` (or **Settings → Cloud Sync → Sync now**). The transport pulls before
   it pushes: exact matches for freshly seeded providers, adapters, and memory are adopted,
   while the remaining cloud records are imported. Local records that are still genuinely new
   may then be uploaded.
5. **Check the result before enabling automation.** Confirm that pending records and conflicts
   are zero, then spot-check projects, memory, model profiles, adapters, and settings in the UI.
   If conflicts appear, leave automatic sync off; the current version retains them for a future
   conflict-resolution workflow rather than silently overwriting either side.
6. **Restore device-specific project locations.** Git repositories and local paths are not
   synchronized. Clone or locate each repository on the new device, then edit the imported
   project's path to point to that checkout. Worktrees, run artifacts, evidence files, and
   rendered memory Markdown remain local; memory Markdown is regenerated from database records.
7. **Enable automatic sync on the new device.** Choose the desired interval in **Settings →
   Cloud Sync**. Enablement and cadence are intentionally independent on each device. Keep
   independent local and cloud backups afterward.

For an additional device that already contains work, back up its SQLite database first and
review the conflict rules above before running synchronization. Simultaneous edits to the same
record are preserved as conflicts, not resolved automatically.

## Implementation phases

1. **Complete:** local UUID mapping, canonical serializers, change detection, and cloud schema.
2. **Complete (offline- and live-verified):** authenticated transport, initial upload/download,
   and dependency-ordered apply.
3. **Complete (offline- and live-verified):** opt-in background automatic synchronization and
   Settings-page status, cadence, and manual-run controls.
4. **Pending:** conflict-resolution UI, deletion tombstones, retention controls, and optional
   encryption.
5. **Optional:** object storage for large artifacts; this is not required for database sync.

The transport currently pulls the current remote record set on each manual or automatic run.
Incremental cursors remain a future optimization.

Synchronization is not a backup. Keep independent local and cloud snapshots so accidental
changes or deletions can be recovered.
