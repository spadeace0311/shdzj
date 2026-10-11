# Final Fix Re-review: 89cec18

## Scope And Method

- Repair base: `c66e2d051dcb877c24aa999d30b996a1cd24da25`
- Implementation commit: `b3ff90f69ade6ac12a1d6781c429b2dc8b53ebcc`
- Report head: `89cec1855e7e7a6148be398e2591f8c1db637f2d`
- Review scope: repair implementation `b3ff90f`, not the complete old branch.
- Method: static, read-only review of the implementation diff and current call paths.
- No test, migration, database, container, Git, index, or HEAD mutation was performed.
- `git diff --check c66e2d0..89cec18` passes.
- The live E2E remains genuinely skipped; this report does not require it to be converted into a false pass.

## Overall Verdict

`NOT ADDRESSED`

Three Critical findings are fixed at their intended normal-path boundaries. Four Important findings remain materially incomplete, and the repair introduces or exposes one Critical and two Important issues.

## Original Item Verdicts

### Critical 1: Legacy Manual Test/Drill Entry

`ADDRESSED`

Evidence:

- `backend/app/events/schemas.py:27` restricts `ManualEventRequest.event_kind` to `manual`.
- `backend/app/events/router.py:99-120` passes that validated value into `EventService.ingest`.
- `frontend/src/pages/ManualEventPage.tsx` no longer exposes test or drill options.
- `backend/tests/test_event_api.py:316-333` verifies the API validation boundary rejects both values.
- `backend/tests/test_operations_priority.py:612-648` repeats this against the real service and an active real event.

No remaining direct legacy bypass was found.

### Critical 2: Correction Must Remain A Real Priority State

`ADDRESSED`

Evidence:

- `backend/app/operations/priority.py:43` includes `correction` in real event types.
- `backend/app/events/service.py:145-150` and `298-303` apply the priority guard for current corrections in both ingest paths.
- `backend/app/events/repository.py:338-348` preserves the original activation time and clears any closed marker when a formal, manual, or correction revision becomes current.
- `backend/app/operations/priority.py:300-315` anchors activity to activation time rather than revision origin time.
- `backend/tests/test_operations_priority.py:548-606` verifies a formal-to-correction transition keeps schedules deferred and queued/running drill runs frozen.

### Critical 3: Non-removable Collaborative And Artifact Markers

`ADDRESSED`

Evidence:

- `backend/app/operations/markers.py:9-59` centralizes mode, filename, and text marker enforcement.
- `backend/app/collaboration/service.py:1508-1527` marks all newly created deliverable versions.
- `backend/app/collaboration/service.py:1629-1665` marks superadmin text/file overrides.
- `backend/app/collaboration/service.py:1449-1488` repairs legacy unmarked versions before publication and copies files to marked immutable names.
- `backend/app/collaboration/router.py:1177-1212` returns the marker on version and publication responses.
- `backend/app/artifacts/service.py:960-999` and `1123-1137` enforce markers and preserve marker metadata on artifact overrides.
- Real API regressions cover upload, publish, override, restore, and artifact override paths.

Automatic artifact-link rows do not carry `operation_marker`, but they have no directly exposed filename/text and point to generated artifacts whose API now carries the marker. This is outside the three original safety bypasses.

### Important 1: Freeze Coverage, Run Reconciliation, And Drill Recovery

`NOT ADDRESSED`

The new freeze calls cover the listed collaboration mutations, and failed runs suppress reminders. Two repair-path defects remain:

1. `backend/app/operations/service.py:401-429` can mark an operations run `completed` when only an `AssessmentRun` with terminal status exists and no `ProductionRun` or `ProductionTask` row was ever created. `all()` over the non-empty assessment status list becomes true. This hides a lost or delayed production workflow instead of leaving the run observable or marking it failed.
2. `backend/app/operations/service.py:295-328` resumes a paused drill by creating an entirely new run but never transitions or clears `resume_required` on the original run. `backend/tests/test_operations_service.py:359-370` explicitly asserts the old run remains paused and eligible. The endpoint also has no idempotency key, so a repeated client retry can create multiple replacement drills while the source run remains shown as recoverable.

The existing focused test only covers the successful terminal-state case with seeded production rows, not the missing-production and repeated-resume cases.

### Important 2: Atomic Boundary Generation And Validation

`NOT ADDRESSED`

Geometry validation and generation-time `ST_Covers` were added, but the production call path is still split across transactions and the version-specific resolver still requires `is_active`.

Evidence:

- `backend/app/operations/scenario.py:33-107` locks an active boundary, validates it, generates the point, and checks `ST_Covers` in one transaction.
- `backend/app/operations/service.py:488-518` commits that transaction before resolving and ingesting.
- `backend/app/regions/service.py:77-81` applies both `is_active IS TRUE` and `version == scenario.boundary_version`.
- `backend/app/events/service.py:347-364` replaces a missing version with the current active version.

Concurrent boundary import can therefore deactivate the locked version immediately after generation commits. The resolver then returns pending, and event ingestion records the new active boundary even though the point was generated and checked against the old boundary. The new test at `backend/tests/test_operations_domain.py:231-267` only calls `region_contains` directly and does not exercise generation, resolver, and ingest under concurrent activation.

### Important 3: Timezone Persistence, Validation, And Error Mapping

`ADDRESSED`

Evidence:

- `backend/app/operations/schemas.py:34-43` validates new API timezone values before service invocation.
- `backend/app/operations/repository.py:60-75` validates persisted timezone values on every settings read.
- `backend/app/operations/priority.py:91-99` prevents an already-invalid persisted timezone from rolling back formal/manual ingestion.
- `backend/app/operations/router.py:87-97` and `217-225` provide stable `invalid_operations_timezone` mappings.
- `backend/tests/test_operations_priority.py:448-473` verifies formal ingestion remains safe with a dirty timezone row.

A recovery defect is recorded separately under New Important 3.

### Important 4: Unified Retention, Active Production Protection, And Storage Cleanup

`NOT ADDRESSED`

The clocks and event-deletion protection are improved, but artifact retention is not protected by active production state:

- `backend/app/artifacts/retention.py:50-70` selects expired generated artifacts without excluding `ProductionRun.status` or active `ProductionTask` rows.
- `backend/app/operations/worker.py:139-150` deletes those artifacts before collaboration retention can protect the event.
- `backend/app/collaboration/retention.py:173-197` protects only event cascade deletion, not the already-completed artifact rows belonging to a still-running mixed production run.

Consequently, a long-running production can lose already-generated artifacts from its database and workflow state while other tasks continue. The focused integration test terminalizes every production row before invoking retention, so it does not exercise this case.

### Important 5: Manual Preemption Of Scheduled Idempotency Key

`ADDRESSED`

Evidence:

- `backend/app/operations/service.py:56-59` reserves both legacy and new scheduler namespaces for manual requests.
- `backend/app/operations/service.py:110` uses a new scheduler-specific key.
- `backend/app/operations/service.py:852-864` rejects reserved manual keys.
- `backend/tests/test_operations_service.py:760-808` verifies preemption is rejected and the scheduler can still trigger.

The upgrade/crash-recovery problem caused by the key namespace change is recorded separately under New Critical 1.

### Important 6: Priority Audit

`ADDRESSED`

Evidence:

- `backend/app/operations/priority.py:116-136` audits schedule deferral.
- `backend/app/operations/priority.py:156-197` audits run cancellation/pause with before/after state and real event identity.
- `backend/app/operations/priority.py:244-275` audits deferred schedule release.
- Focused tests assert the audit rows and state payloads in real sessions.

### Important 7: Artifact Override Freeze And Real-event Guard

`ADDRESSED`

Evidence:

- `backend/app/artifacts/service.py:953-968` loads the event with a row lock and checks the target operation run plus active real-event state before creating the override production run.
- `backend/app/artifacts/router.py:625-641` maps both guards to HTTP `409`.
- Service and API regressions cover paused and active-real rejection.

### Important 8: Activation Time Cannot Be Backfilled Through Origin Time

`ADDRESSED`

Evidence:

- `backend/app/events/repository.py:338-348` persists ingestion-time activation.
- `backend/app/operations/priority.py:300-315` uses persisted activation time and never `origin_time`.
- `backend/tests/test_operations_priority.py:476-542` verifies eight-day-old formal and manual events are active immediately upon current ingestion.

### Important 9: Live E2E Skip Honesty And Health URL Correction

`NOT ADDRESSED`

The skip is honest and the report does not claim a pass. The health URL correction is still broken in its documented fallback:

- `frontend/e2e/operations.spec.ts:4-9` uses nullish coalescing after converting an absent `E2E_API_BASE_URL` to an empty string.
- When `E2E_API_BASE_URL` is unset, `API_HEALTH_URL` becomes `""`, so `request.get("/health")` can again target the frontend origin.
- `README.md:255` and `docs/runbooks/operations-test-drill.md:431` claim it falls back to `http://127.0.0.1:8000`, which the implementation does not do.

The explicit skip at `frontend/e2e/operations.spec.ts:25-30` is acceptable, but the script is not fully corrected.

### Important 10: Heartbeat-backed Compose Health Check

`ADDRESSED`

Evidence:

- `backend/app/operations/health.py:31-35` atomically writes a successful-cycle heartbeat.
- `backend/app/operations/health.py:53-75` makes the file unhealthy after a stale heartbeat.
- `backend/app/operations/health_check.py:10-21` exposes the file check as a CLI health command.
- `backend/app/operations/worker.py:232-241` does not refresh the heartbeat after a failed cycle.
- `infra/compose.yaml:303-333` uses the heartbeat file with suitable health timing.
- Tests cover fresh/stale heartbeat files and repeated cycle failures.

## New Critical Or Important Findings

### New Critical 1: Scheduled Key Rename Can Delete An Existing Event During Upgrade Recovery

`backend/app/operations/service.py:110` changes the scheduler key from the repair-base `scheduled:<schedule_id>` to `operations-scheduler:<schedule_id>`, but no migration or compatibility lookup recognizes an in-flight old row. The scheduler commits a run before marking the schedule triggered at `backend/app/operations/scheduler.py:218-274`; a crash between those steps can leave a `due` schedule with an existing old-key run.

After upgrade, the new code misses that existing run at `backend/app/operations/service.py:112`, attempts another ingest against the same canonical scheduled event, then hits the unique `schedule_id` constraint at `backend/app/operations/models.py:174`. The `ValueError` path at `backend/app/operations/service.py:691-693` calls `_delete_rejected_event`, whose implementation at `715-730` deletes the entire earthquake event and cascades its runs and related rows.

This is a destructive idempotency upgrade path and must be fixed before deployment.

### New Important 1: Operations Worker Ignores `artifact_retention_enabled`

`backend/app/config.py:74` still exposes `artifact_retention_enabled`, and `backend/app/artifacts/worker.py:1048-1059` honors it. The new integrated path in `backend/app/operations/worker.py:123-150` calls `ArtifactRetentionService.retain_expired` unconditionally whenever operations retention is due.

Setting `artifact_retention_enabled=false` therefore no longer protects test/drill artifact rows or files once the operations worker is enabled. This is a long-term retention regression not covered by the new integration test.

### New Important 2: An Already-invalid Persisted Timezone Cannot Be Repaired Through The API

`backend/app/operations/repository.py:97` reads and validates the existing row before applying the update patch. If that row already contains an invalid timezone, `_validate_timezone` at `326-332` raises `invalid_operations_timezone`, so even a superadmin request that supplies a valid replacement timezone is rejected. The API maps it to HTTP `503`, leaving the operations worker unhealthy until an external database edit is performed.

The fix prevents new invalid writes and preserves formal ingestion, but it does not provide the recovery path needed for data created by the bug this item is fixing.

## Verification Limits

- No suites were rerun by this reviewer because running the shared migration, backend, Temporal, or Compose tests could alter database/container state.
- The evidence in `final-review-fix-report.md` was treated as claimed prior execution evidence, not independently repeated.
- No new credential, token, connection string, or literal secret was found in the repair diff.
- No non-`codex-operations` destructive deployment command was introduced.
