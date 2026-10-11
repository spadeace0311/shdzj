# Operations Safety Residuals Final Report

## Status

- Branch: `codex/operations-automation`
- Base: `670e037`
- Code verification head: `b8d7621bd8b74dbbfa6925402d7d69ab29788be0`
- Whole-branch review package:
  `.superpowers/sdd/2026-10-11-operations-safety-residuals/final-review-670e037..b8d7621.diff`
- Whole-branch verdict: `READY` after one fix round
- Push: pending

## Scope

This fix wave closes the residual Critical and Important findings recorded in
`.superpowers/sdd/2026-10-10-operations-test-drill-automation/re-review-89cec18.md`
without reworking the already-approved operations workflows. Four low-risk
Minors remain deferred and are listed at the end of this report.

## Commit List

| Commit | Subject |
| --- | --- |
| `577a64c` | `docs: plan operations safety residual fixes` |
| `8a7c55b` | `fix: recover legacy scheduled operations runs` |
| `3fcfe43` | `fix: harden legacy scheduler recovery` |
| `0b0ef4a` | `fix: make scheduled operation ingest transactional` |
| `ecaae38` | `docs: record task 1 residual completion` |
| `8299ba3` | `fix: reconcile operations runs and resume drills idempotently` |
| `a1b6718` | `fix: harden drill resume and reconciliation audits` |
| `35693c9` | `fix: remove resume replay reverse lock order` |
| `809e43c` | `docs: record task 2 residual completion` |
| `00c5b51` | `fix: preserve generated scenario boundary context` |
| `12e4a6f` | `fix: protect active artifacts from retention` |
| `d847e07` | `fix: validate precomputed scheduled scenarios` |
| `2ac68a9` | `fix: correct operations e2e health fallback` |
| `a69f5ec` | `fix: allow repair of invalid operations timezone` |
| `ae4ef2e` | `fix: protect active artifacts from retention` |
| `fcd42d9` | `fix: align artifact preparation lock order` |
| `b8d7621` | `fix: make manual operations run creation atomic` |

## Residual Finding Disposition

### New Critical 1: scheduled key rename can delete an existing event

`ADDRESSED`

- Legacy `scheduled:<schedule_id>` and current
  `operations-scheduler:<schedule_id>` runs are both recognized before ingest.
- Schedule-run creation, collaboration dispatch, and assessment dispatch now
  commit through one transaction.
- A conflict with an existing event or run rolls the complete ingest back;
  there is no post-commit event/revision/outbox deletion path.
- Real PostgreSQL regressions cover legacy-key recovery, crash windows,
  concurrent services, active-real-event conflicts, and forced run-persist
  failure.

### Original Important 1: freeze coverage, reconciliation, and drill recovery

`ADDRESSED`

- An assessment that is terminal without production rows remains `running` and
  emits a deduplicated `production_pending` reconciliation audit.
- A failed or canceled assessment still freezes the operations run as failed.
- Drill resume uses the deterministic key `resume:<source_run_id>`.
- The first successful resume creates one replacement, cancels and clears the
  source run, and stores the replacement linkage. Repeated and concurrent
  resumes return the same replacement.
- Resume and artifact override now use a consistent lock order.
- Worker regressions suppress deadline reminders for frozen runs.

### Original Important 2: atomic boundary generation and validation

`ADDRESSED`

- Manual, drill, and scheduled generation create region context inside the
  transaction that locks and validates the boundary version.
- Event ingestion receives that context directly instead of resolving the
  current active boundary after the generation transaction commits.
- Precomputed scheduled scenarios are reconstructed inside the scheduled
  transaction and checked with version-aware PostGIS `ST_Covers`.
- Outside or unknown-boundary scheduled snapshots fail before event, revision,
  raw-message, outbox, or operations-run rows are created.
- Service regressions assert the persisted boundary version,
  `inside_shanghai`, zero boundary distance, and `region_computed_at` across a
  committed boundary replacement.

### Original Important 4: retention and active production protection

`ADDRESSED`

- Artifact retention excludes an entire candidate event while any
  `ProductionRun` is `pending` or `running`, or any `ProductionTask` is
  `pending`, `ready`, or `running`.
- Candidate events, all their production runs, and tasks are locked and
  rechecked before deletion.
- Artifact retention and collaboration retention run in one transaction, so a
  collaboration failure rolls artifact row deletion and cleanup intents back.
- Artifact preparation now follows the shared `event -> run -> task` lock
  order, removing the event/run deadlock cycle found during re-review.
- Real PostgreSQL regressions cover active same-event rebuilds, old final
  artifact dependencies, concurrent activation, transaction rollback, and the
  two-session lock-order race.

### New Important 1: operations worker ignores the artifact retention flag

`ADDRESSED`

- `artifact_retention_enabled=false` skips artifact `retain_expired`.
- Collaboration retention and already-enqueued object cleanup intents still
  run.
- Retention counters remain zero while artifact retention is disabled.

### New Important 2: invalid persisted timezone cannot be repaired

`ADDRESSED`

- The update path loads an invalid persisted timezone without validating it,
  checks the expected row version, applies the normalized patch, and validates
  the resulting value before mutation.
- A missing or invalid repair still raises the stable
  `invalid_operations_timezone` error.
- Ordinary reads retain the existing stable error behavior.
- The superadmin API regression repairs a real invalid database row and audits
  the before/after state and row-version change.

### Original Important 9: live E2E health URL fallback

`ADDRESSED`

- URL precedence is non-empty `E2E_API_HEALTH_URL`, non-empty
  `E2E_API_BASE_URL`, then `http://127.0.0.1:8000`.
- Empty and whitespace-only values fall back correctly; trailing slashes are
  normalized.
- Missing real credentials or missing `E2E_ALLOW_EVENT_PURGE=1` still produce
  an explicit skip, never a false pass.

### Final whole-branch Important 1: manual/drill run creation crash window

`ADDRESSED`

- Manual test and drill run creation now uses the same
  `EventService.ingest_collected` transaction callback pattern as scheduled
  tests.
- Priority, idempotency, and run-conflict checks run before lifecycle,
  collaboration, or assessment side effects are committed.
- The callback atomically creates the operations run with the event,
  revision, raw message, roster snapshot, and outboxes. Commit-before-run
  crash windows now roll the complete ingest back on retry.
- The old post-commit finalization and destructive event cleanup path was
  removed.
- Real PostgreSQL regressions cover a rootless work rollback, legacy
  missing-run repair, the exact crash-then-real-event retry order, outbox/task
  counts, and the scheduler crash window.

## Verification Evidence

Backend final focused suite:

```text
pytest tests/test_operations_service.py tests/test_operations_scheduler.py \
  tests/test_operations_worker.py tests/test_operations_api.py \
  tests/test_operations_domain.py tests/test_region_resolver.py \
  tests/test_artifact_retention.py tests/test_operations_repository.py -v

138 passed, 15 warnings in 62.60s
```

Migration and schema round-trip suite:

```text
pytest tests/test_migrations.py tests/test_assessment_schema.py \
  tests/test_intensity_schema.py tests/test_data_asset_schema.py \
  tests/test_event_lifecycle_schema.py tests/test_collector_schema.py \
  tests/test_qa_audit_schema.py -v

35 passed in 899.52s
```

Frontend:

```text
npm test
24 test files passed, 223 tests passed

npm run typecheck
passed

npm run build
passed
```

The build reports the existing `maplibre-gl` chunk warning at `938.88 kB`;
there are no build errors.

The final fix commit `fcd42d9..b8d7621` changes only
`backend/app/operations/service.py` and
`backend/tests/test_operations_service.py`; the migration/schema and frontend
artifacts verified immediately before that commit are unchanged.

Ruff over all changed Python production and test files: `All checks passed!`

`git diff --check 670e037..b8d7621`: passed.

The independent whole-branch reviewer reran its broad backend set with
`154 passed` before the final fix, then re-reviewed
`fcd42d9..b8d7621` and returned `READY` with `0 Critical / 0 Important`.

The live operations Playwright E2E was not executed because the required real
superadmin credential and explicit
`E2E_ALLOW_EVENT_PURGE=1` authorization were not both available. The skip
behavior itself was reviewed and exercised; no live pass is claimed.

## Deferred Minors

1. A successful scheduled commit can be reported as failed if the separate
   result reread transiently fails after commit. The run exists, so this is an
   observability/retry-state issue rather than data corruption.
2. Task 3 has a low-risk test gap for a successful precomputed JSONB scenario
   and a separately named unknown-boundary-version case.
3. Historical precomputed scenarios are geometry-checked against the recorded
   version but are not revalidated with `ST_IsValid`; geometry validity is
   still established at generation/import time.
4. The Task 5 API repair regression leaves its generated audit row in the
   shared test database.
5. The E2E documentation says an unavailable API is skipped, while a TCP
   connection refusal makes Playwright fail. A non-200 reachable response does
   skip, and a connection failure still cannot produce a false pass.

These Minors do not block deployment or merge readiness.

## Verification Limits

- No production or acceptance database was destructively modified.
- All Docker verification used only project `codex-operations`.
- No credential, token, API key, password, connection string, or `.env`
  content is included in this report or the review package.
- The whole-branch review package excludes this report and the ledger update;
  the code head reviewed after the final fix is `b8d7621`.
