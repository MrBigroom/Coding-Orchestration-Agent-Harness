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
- Worker/scheduler enablement, parallelism limits, and permission to run GUI commands
- Automatic-sync enablement and cadence, which are configured independently on each device
- Git repositories, worktrees, run artifacts, and SQLite WAL files

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
