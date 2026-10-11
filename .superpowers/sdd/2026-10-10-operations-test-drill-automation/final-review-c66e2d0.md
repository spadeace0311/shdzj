# Final Branch Review: 6d55b8e..c66e2d0

## Scope And Evidence

- Base: `6d55b8e46d76d57eb1c8d0e2edc501b0a41aa22b`
- Head: `c66e2d051dcb877c24aa999d30b996a1cd24da25`
- Branch: `codex/operations-automation`
- Reviewed statically across the complete branch diff plus unchanged integration surfaces that affect the new invariants.
- `git diff --check` passed.
- I did not rerun the supplied suites or invoke any command that could change database or container state.
- Prior evidence is treated as evidence for the executed suites only: backend `1746 passed, 3 skipped, 8 deselected`; migration/operations `32 passed`; frontend `216 passed`; operations E2E `1 skipped`.

### Strengths

- The database uses strong identity constraints for the core scheduling path: one active settings row, one schedule per `local_date`, one run per event/schedule/idempotency key, and cascading test/drill event cleanup.
- Schedule creation, claiming, triggering, real-event deferral, and release are serialized with the appropriate unique constraints, row locks, advisory locks, and `SKIP LOCKED`; the raw-message deduplication also makes crash retries substantially safer.
- Automatic map/document/PPT production has real marker generation, content validation, and immutable `production_mode` checks. Test and drill names are generated with the required prefix.
- The new manual-event service path starts the complete assessment and collaboration outboxes for `EventKind.MANUAL`; collected formal and manual events both invoke the priority guard in their initial transaction.
- Operations mutation permissions are enforced server-side: settings require superadmin; manual test/drill require superadmin, group leader, or group deputy; read endpoints require authentication.
- Retention selects only `event_type IN ('test', 'drill')`, so formal, manual, auto, and correction rows are not directly selected by the event retention service.
- The new operations runbook commands consistently use `--project-name codex-operations`; no literal password, token, key, connection string, or credential material was found in the reviewed diff.
- Migration registration, upgrade/downgrade order, default settings row insertion, and cascade verification are coherent and covered.

### Issues

#### Critical (Must Fix)

1. **Legacy manual-event API still creates test/drill events outside every new guard.**
   - `backend/app/events/schemas.py:27` accepts `manual|test|drill`.
   - `backend/app/events/router.py:99-120` passes that value directly into `EventService.ingest`.
   - `backend/app/events/service.py:92-153` only applies boundary resolution, assessment/collaboration outboxes, and the priority guard when the kind is `MANUAL`.
   - `frontend/src/pages/ManualEventPage.tsx:158-167` still exposes test and drill choices.
   - `backend/tests/test_event_api.py:288-313` explicitly codifies this bypass as expected success.
   - Failure: an authorized group leader/deputy can create arbitrary-coordinate test/drill events during an active real event, with no `OperationsRun`, no operations idempotency, no operations audit, and no guarantee of the complete assessment workflow or marker-safe place text.
   - Risk: direct violation of real-event priority, Shanghai-boundary, dedicated-entry, marker, audit, and retention invariants.
   - Fix: restrict this schema/controller to `manual`; remove the legacy UI options; add a regression proving test/drill are rejected even under real-event activity.

2. **A correction replaces the current event type and drops real-event priority protection.**
   - `backend/app/events/repository.py:885` writes the current revision kind into `earthquake_events.event_type`.
   - `backend/app/events/lifecycle.py:42-49` classifies the second reviewed report as `correction`.
   - `backend/app/operations/priority.py:35` defines real event types as only `formal|manual`, and `priority.py:217-225` filters on `EarthquakeEvent.event_type`.
   - `backend/app/events/service.py:297-305` invokes `apply_real_event_start` only for `FORMAL|MANUAL`, not `CORRECTION`.
   - Failure: after a formal event receives a current correction, `has_active_real_event()` becomes false. Deferred schedules can be released, new test/drill operations can trigger, and the correction does not re-freeze queued/running non-formal runs.
   - Risk: a routine CENC correction reopens the seven-day test/drill window during an active real response.
   - Fix: treat current `formal|manual|correction` as real priority states, invoke the guard for current corrections, and add a formal-to-correction regression that verifies schedules and runs remain frozen.

3. **Test/drill markers are not enforced across collaborative and override output paths.**
   - `backend/app/collaboration/service.py:1112-1125` stores uploaded filenames unchanged.
   - `backend/app/collaboration/service.py:1177-1224` and `1526-1574` publish superadmin override text/files unchanged.
   - `backend/app/collaboration/router.py:671-700` and `821-892` pass caller-provided filenames through.
   - `backend/app/artifacts/service.py:516` and `1078-1087` preserve the uploaded artifact override filename.
   - `backend/app/artifacts/repository.py:1009-1015` can persist a generated artifact with no marker metadata for an override.
   - Failure: a group member can upload a test/drill collaborative file named `正式专报.docx`, a superadmin can publish arbitrary unmarked text/files, and an artifact override can expose an unmarked filename. The collaborative API response itself has no event-kind/marker field.
   - Risk: test/drill output can be downloaded, exported, or presented as formal data.
   - Fix: enforce a mode-aware filename/text wrapper in one central service layer for every upload, override, publish, restore, and export path; return the marker with API responses; add real integration tests for all mutation routes.

#### Important (Should Fix)

1. **The collaboration freeze is incomplete, and operation runs have no terminal reconciliation or resume path.**
   - `backend/app/collaboration/service.py:94-139` (`start`), `239-281` (`complete`), `331-390` (`update`), and `630-776` (temporary create/update) do not call `ensure_event_work_allowed`.
   - `backend/app/operations/priority.py:38` freezes only `paused|canceled`; `backend/app/collaboration/scheduler.py:156-163` also excludes only those two states.
   - `backend/app/operations/service.py:513` creates every run as `queued`, and only `priority.py:123-139` ever changes it to paused/canceled. `completed` and `failed` are never assigned.
   - `resume_required=true` has no authorized API/UI path back to a runnable state.
   - Risk: users can still start/update/complete or create temporary work after cancellation; failed runs would continue deadline processing; every successful test/drill remains permanently queued in the UI and is later treated as an active run by the real-event guard.
   - Fix: guard all task mutations, reconcile operations state from assessment/artifact terminal state, add explicit resume semantics for paused drills, and cover failed-state reminder suppression.

2. **Scenario generation does not validate geometry or atomically verify that the point remains inside the current active boundary.**
   - `backend/app/operations/scenario.py:24-37` trusts `ST_PointOnSurface` without checking `ST_IsValid`, `ST_IsEmpty`, or `ST_Area`.
   - `backend/app/operations/scenario.py:81-100` implements `region_contains`, but production generation never calls it before ingest.
   - `backend/app/operations/service.py:373-403` commits/closes the generation transaction, then `service.py:330-333` resolves region context in a separate session; scheduled triggering has the same split at `service.py:405-424`.
   - `backend/app/regions/importer.py:164-179` validates ring size and antimeridian segments but not ring closure, self-intersection, or zero-area/topology validity.
   - Risk: an invalid or degenerate active boundary can produce an unusable point or a generic storage error; concurrent boundary activation can generate against old geometry and resolve against new geometry, creating an event outside the current boundary.
   - Fix: generate, validate, resolve, and `ST_Covers`-check against one locked boundary version in the same transaction; persist that version on the scenario/event.

3. **A superadmin can persist an invalid timezone and break formal ingestion or the worker.**
   - `backend/app/operations/repository.py:137-141` accepts any non-empty timezone string.
   - `backend/app/operations/scheduler.py:106`, `priority.py:82-83`, and `service.py:225` call `ZoneInfo` later without validation.
   - `backend/app/events/service.py:145-152` performs the priority guard inside the formal/manual event transaction.
   - Risk: `ZoneInfoNotFoundError` can roll back an entire formal/manual event and continuously crash the operations worker. The trigger API maps this as `operations_storage_unavailable`, hiding the configuration error.
   - Fix: validate `ZoneInfo` in the repository/schema update path, add a stable configuration error mapping, and test both worker and formal ingestion behavior.

4. **Event and artifact retention use different clocks, cleanup paths, and active-dependency protections.**
   - `backend/app/collaboration/retention.py:44-50` selects by event `origin_time`; `151-157` uses calendar two-year arithmetic and `<=` for test cutoff.
   - `backend/app/artifacts/retention.py:44-64` selects by `GeneratedArtifact.generated_at`, uses 730 fixed days for drill, and uses `<`.
   - `backend/app/operations/worker.py:95-105` deletes event rows without enqueueing storage cleanup; `backend/app/artifacts/worker.py:1048-1062` runs independently.
   - `backend/app/collaboration/retention.py:115-149` protects workgroup tasks/outboxes/assessment runs but not pending/running `ProductionRun` or `ProductionTask` rows.
   - Risk: calendar and fixed-day boundaries can delete an event before its files or vice versa; manual deliverable files become unreachable orphans; an overdue-but-still-producing event can be cascade-deleted while Temporal activities continue.
   - Fix: unify retention clocks and cutoffs, protect active production tasks, and run event deletion through storage-aware cleanup with an idempotent receipt.

5. **A caller can preempt the scheduler's reserved idempotency key and permanently fail the daily test.**
   - `backend/app/operations/service.py:109` reserves `scheduled:<schedule_id>` for the worker.
   - `service.py:293-316` and `680-688` accept that namespace from manual test/drill requests.
   - `backend/app/operations/router.py:125-129` exposes schedule IDs to every authenticated user, and trigger permissions include group leader/deputy.
   - `backend/tests/test_operations_service.py:631-675` asserts that the resulting daily trigger fails.
   - Risk: an authorized caller can create a manual test with the scheduler key before the daily window, causing `trigger_scheduled_test` to reject the legitimate run and the scheduler to mark the day failed.
   - Fix: reject reserved `scheduled:` keys at manual API validation, use a non-colliding scheduler namespace/table key, and add an end-to-end preemption regression.

6. **Real-event priority transitions and deferred-release changes are not recorded in the operations audit table.**
   - `backend/app/operations/priority.py:63-142` updates schedules/runs, timestamps, `detail.reason`, and `resume_required` without adding an `OperationsAuditLog`.
   - `priority.py:174-198` releases deferred schedules without audit records.
   - Risk: the audit surface cannot show who/which event caused a pause/cancel/release or reconstruct the before/after state despite the dedicated audit model.
   - Fix: write `operations.priority.*` audit rows in the same transactions and verify them in the existing concurrency tests.

7. **Superadmin artifact overrides are not blocked while the event run is frozen or a real event is active.**
   - `backend/app/artifacts/service.py:484-516` and `artifacts/router.py:582-606` create a new standalone production run without consulting `OperationsPriorityGuard`.
   - Risk: after real-event priority has paused/canceled a test/drill operation, a superadmin can still override an artifact for that event and create new test/drill output.
   - Fix: call `ensure_event_work_allowed`/the active-real-event guard under the existing event lock and add API regression coverage.

8. **The seven-day window is anchored only to caller-controlled `origin_time`, so a late or backdated manual event can bypass activation.**
   - `backend/app/operations/priority.py:223-225` treats only `origin_time` in the last seven days as active.
   - `backend/app/events/schemas.py:18-27` permits arbitrary past `origin_time` values on the manual entry.
   - Risk: a group leader can enter a real manual event with an eight-day-old origin time and immediately perform tests/drills despite the event just becoming active.
   - Fix: introduce an explicit activation/close marker or derive activation from current-revision ingestion time, validate manual timing policy, and test late-ingested formal/manual events.

9. **The only live operations E2E is skipped, leaving the full real-backend workflow unverified.**
   - `frontend/e2e/operations.spec.ts:20-25` skips unless both the real password and explicit purge gate are present.
   - No run with those variables was produced; the actual evidence is `1 skipped`.
   - Risk: the real login-to-39-artifacts-to-priority-to-retention flow, including all 27 maps and 12 report/deck outputs, remains unexecuted end to end.
   - Fix: run the E2E in the designated disposable `codex-operations` environment with real credentials and purge authorization before declaring the acceptance complete.

10. **The deployed worker health check reports process existence rather than successful loop progress.**
    - `infra/compose.yaml:312-315` runs `python -m app.process_health operations`.
    - `backend/app/process_health.py:15-24` only checks command-line process presence, while `backend/app/operations/health.py:19-35` defines a heartbeat model that is not exposed to Compose.
    - Risk: a worker whose cycle repeatedly fails but whose process remains alive can be reported healthy, silently stopping scheduling and retention.
    - Fix: expose the heartbeat state on a loopback endpoint or status file, configure Compose with normal health timing, and test both stale-heartbeat and repeated-cycle-failure behavior.

#### Minor (Nice to Have)

- Trigger routes map all unknown exceptions to storage-unavailable (`backend/app/operations/router.py:173-195`), which can hide programming/configuration faults.
- Idempotent trigger replay appends another `operations.*.triggered` audit row (`backend/app/operations/service.py:604-642`).
- Operations trigger buttons remain visible to roles that receive 403 (`frontend/src/pages/OperationsPage.tsx:395-442`).
- The runbook's unauthenticated `Invoke-RestMethod` example immediately warns that the endpoint requires a bearer token (`docs/runbooks/operations-test-drill.md:67-75`).
- CPython-specific random-sequence expectations and a small number of timing-based concurrency tests remain portability/flake risks.
- `OperationsTestSchedule.scenario` is never populated by the backend even though the schedule API exposes it.

### Deferred Minor Triage

| Deferred item / ruling | Final decision |
| --- | --- |
| Task 1: duplicate `OPERATIONS_TABLES` constants | Non-blocking |
| Task 1: cascade deletion of schedules/runs with retained test/drill events | Accepted |
| Task 2: settings initialization lacks deterministic concurrency barrier | Non-blocking; the database uniqueness path is stronger than the test barrier |
| Task 2: CPython random-sequence portability | Non-blocking |
| Task 3: broad unknown-exception mapping | Non-blocking alone, but it worsens the invalid-timezone finding |
| Task 3: duplicate trigger audit row on replay | Non-blocking |
| Task 3: missing direct positive superadmin/deputy permission tests | Non-blocking |
| Task 4: disabled schedules counted as due | Non-blocking |
| Task 4: weak cross-date variation assertion | Non-blocking |
| Task 5: timezone validation | **Blocking; covered by Important finding 3** |
| Task 5: duck-typed query-session no-op | Non-blocking |
| Task 5: additional direct upload/override/restore 409 coverage | **Blocking as part of freeze coverage, Important finding 1** |
| Task 5: seven-day `origin_time` ruling | **Blocking without activation-time policy; Important finding 8** |
| Task 5: manual-event full assessment and router 409 rulings | Accepted |
| Task 6: INFO-level failure logging | Non-blocking |
| Task 6: no run-state reconciliation ruling | **Blocking; Important finding 1** |
| Task 6: deployment health is process liveness only | **Blocking for reliable unattended operations; covered by Important finding 10** |
| Task 6: healthcheck interval/timeout polish | Non-blocking |
| Task 6: upstream Starlette warning | Non-blocking |
| Task 7: unauthorized trigger buttons remain visible | Non-blocking |
| Task 8: E2E health URL can target frontend origin | Non-blocking but should be corrected with the live E2E run |
| Task 8: active-real-event runbook precision | Non-blocking after correction behavior is fixed and documented |
| Task 8: pause wording | Addressed |
| Live operations E2E skip | **Blocking verification gap; Important finding 9** |

### Assessment

**Ready to merge?** No

**Reasoning:** The branch has solid persistence and normal-path coverage, but the legacy manual test/drill route and correction handling create direct real-event-priority bypasses, while collaborative override paths can remove the required safety markers. These are release-safety violations and must be fixed before merge.
