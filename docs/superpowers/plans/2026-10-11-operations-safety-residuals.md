# Operations Safety Residuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the residual Critical and Important findings from `re-review-89cec18.md` without changing the approved operations workflows.

**Architecture:** Keep the existing operations scheduler, run model, scenario generator, retention services, and collaborative marker system. Add compatibility and recovery paths inside those services, use one transaction for geometry validation, and add focused regression tests before each production change.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL/PostGIS, Alembic, Pytest, React/TypeScript, Vitest, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

**Residual Review:** `.superpowers/sdd/2026-10-10-operations-test-drill-automation/re-review-89cec18.md`

## Global Constraints

- The production implementation remains on branch `codex/operations-automation`.
- The only Docker Compose project used for verification is `codex-operations`.
- Never read, print, commit, or expose `.env`, passwords, tokens, API keys, connection strings, or real credentials.
- Preserve all existing test markers and long-term retention rules.
- Do not weaken authentication, priority guards, or marker enforcement to make a test pass.
- Every task starts with a failing regression test, then a minimal implementation, focused verification, and one commit.
- PostGIS tests must use the existing session fixtures and migration head; do not add a second migration unless a schema change is actually required.

---

### Task 1: Recover a pre-existing scheduler run after the idempotency-key upgrade

**Files:**
- Modify: `backend/app/operations/service.py`
- Test: `backend/tests/test_operations_service.py`
- Test: `backend/tests/test_operations_scheduler.py`

**Interfaces:**
- Consumes: `OperationsRun.idempotency_key`, `OperationsRun.schedule_id`, `OperationsTestSchedule.id`, `EventService.ingest_collected`.
- Produces: scheduler compatibility lookup that recognizes both `scheduled:<schedule_id>` and `operations-scheduler:<schedule_id>`; safe conflict recovery that never deletes the event referenced by the existing scheduled run.

- [ ] **Step 1: Write a failing legacy-key recovery test**

Create a due schedule and a scheduled event, insert an `OperationsRun` with
`idempotency_key=f"scheduled:{schedule.id}"`, then call
`trigger_scheduled_test(schedule.id, "operations-scheduler")`.

Assert that the returned `event_id` and `run_id` are the pre-existing values,
the event count remains one, the legacy run remains present, and no new run or
test event is created.

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py -k legacy_scheduler -v
```

Expected: the old-key run is missed, so the trigger attempts a second event
and returns a conflict or creates a duplicate.

- [ ] **Step 3: Add compatibility lookup and non-destructive recovery**

Add a helper that looks up a scheduled run by the current key first and the
legacy key second, validating `trigger_mode` and `schedule_id`. Before any
ingest, return the existing run when it is already valid.

In the insert-conflict branch of `_finalize_trigger`, look up the existing run
by `schedule_id` before deleting the newly ingested event. If an existing run
is found, delete only the new duplicate event, reuse the existing run, and
return its stored revision. Never call `_delete_rejected_event` on the event
owned by the existing run.

- [ ] **Step 4: Add the scheduler crash-window regression**

In `test_operations_scheduler.py`, simulate a due schedule with an existing
legacy-key run and no schedule `event_id`. Dispatch the schedule and assert
that it becomes `triggered`, points at the existing event, and does not create
a second operations run.

- [ ] **Step 5: Run focused tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py tests/test_operations_scheduler.py -v
```

Commit with `fix: recover legacy scheduled operations runs`.

---

### Task 2: Reconcile terminal runs and make drill resume idempotent

**Files:**
- Modify: `backend/app/operations/service.py`
- Test: `backend/tests/test_operations_service.py`
- Test: `backend/tests/test_operations_api.py`
- Test: `backend/tests/test_operations_worker.py`

**Interfaces:**
- Consumes: `AssessmentRun`, `ProductionRun`, `ProductionTask`, `OperationsRun.resume_required`, `OperationsRun.detail`.
- Produces: reconciliation that keeps a completed assessment with no production rows observable as `running`; deterministic resume key `resume:<source_run_id>`; source run terminalization and `detail["resumed_by_run_id"]` linkage.

- [ ] **Step 1: Write failing missing-production and repeated-resume tests**

Extend the reconciliation test so an assessment is terminal but no production
run or task exists. Assert the operations run remains `running`, not
`completed`.

Extend the resume test so two sequential calls for the same paused drill return
the same replacement run, the source run becomes `canceled`, and
`resume_required` is false after the first successful resume.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py -k "reconciliation or resume" -v
```

Expected: missing production currently becomes `completed`, and repeated
resume creates a second replacement.

- [ ] **Step 3: Implement reconciliation and idempotent resume**

When assessment statuses are terminal but no production rows exist, leave the
operations run `running` and audit a `production_pending` reconciliation
reason. A failed or canceled assessment still transitions the run to `failed`.

Use `resume:<source_run_id>` as the new run idempotency key. After the
replacement exists, lock and update the source run to `canceled`, clear
`resume_required`, set `canceled_at`, and store `resumed_by_run_id` in
`detail`. A repeated resume returns the linked replacement without creating
another event.

- [ ] **Step 4: Add API and worker assertions**

The API regression must prove repeated POSTs are idempotent. The worker
regression must prove a failed/canceled run produces no deadline reminders and
that a pending production workflow is not reported completed.

- [ ] **Step 5: Run focused tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py tests/test_operations_api.py tests/test_operations_worker.py -v
```

Commit with `fix: reconcile operations runs and resume drills idempotently`.

---

### Task 3: Bind scenario generation and region context to one boundary version

**Files:**
- Modify: `backend/app/operations/scenario.py`
- Modify: `backend/app/operations/service.py`
- Test: `backend/tests/test_operations_domain.py`
- Test: `backend/tests/test_operations_service.py`
- Test: `backend/tests/test_region_resolver.py`

**Interfaces:**
- Consumes: locked `RegionBoundary`, `OperationsScenario.boundary_version`, `RegionContext`.
- Produces: `ScenarioGenerator.region_context(scenario, computed_at)` returning `inside_shanghai=True`, zero boundary distance, and the locked boundary version.

- [ ] **Step 1: Write a failing version-handoff test**

Generate a scenario in a transaction, deactivate its boundary before the
region resolver is called, and assert that the operations service still
ingests the event with the scenario's original boundary version and
`inside_shanghai=True`.

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py -k boundary_version -v
```

Expected: the current resolver either returns pending or substitutes the new
active boundary.

- [ ] **Step 3: Produce the context inside the generation transaction**

Add `ScenarioGenerator.region_context(...)` and return the generated scenario
plus that context from the manual and scheduled preparation paths. Pass the
context directly into `ingest_collected`; do not call
`RegionContextResolver.resolve` after the generation transaction commits.

- [ ] **Step 4: Add direct generator tests**

Assert the context has the locked version, zero distance, and
`inside_shanghai=True`, including after the boundary is deactivated in a later
transaction.

- [ ] **Step 5: Run focused tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_domain.py tests/test_operations_service.py tests/test_region_resolver.py -v
```

Commit with `fix: preserve generated scenario boundary context`.

---

### Task 4: Protect active production artifacts and honor the artifact retention flag

**Files:**
- Modify: `backend/app/artifacts/retention.py`
- Modify: `backend/app/operations/worker.py`
- Test: `backend/tests/test_artifact_retention.py`
- Test: `backend/tests/test_operations_worker.py`

**Interfaces:**
- Consumes: `ProductionRun.status`, `ProductionTask.status`, `configured.artifact_retention_enabled`.
- Produces: artifact retention that skips runs with `pending|running` status or tasks with `pending|ready|running` status; integrated operations worker calls `retain_expired` only when the flag is true.

- [ ] **Step 1: Write failing active-production and disabled-flag tests**

Seed an expired test artifact whose production run is running or whose task is
pending, run `retain_expired`, and assert the artifact survives. Terminalize
the dependency and assert a later run removes it. Add a worker test with
`artifact_retention_enabled=False` and assert `retain_expired` is not called.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_artifact_retention.py tests/test_operations_worker.py -k "active_production or disabled" -v
```

Expected: the active artifact is currently selected and the operations worker
currently invokes retention unconditionally.

- [ ] **Step 3: Add dependency predicates and flag handling**

Exclude active production runs with a `not exists` predicate for active
production tasks. In the operations worker, skip `retain_expired` when the
configuration flag is false while still processing already-enqueued cleanup
intents according to the existing safe worker behavior.

- [ ] **Step 4: Run focused tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_artifact_retention.py tests/test_operations_worker.py -v
```

Commit with `fix: protect active artifacts from retention`.

---

### Task 5: Allow a superadmin to repair an invalid persisted timezone

**Files:**
- Modify: `backend/app/operations/repository.py`
- Test: `backend/tests/test_operations_repository.py`
- Test: `backend/tests/test_operations_api.py`

**Interfaces:**
- Consumes: `OperationsSettings.timezone`, `OperationsSettingsUpdateRequest.timezone`, `StaleOperationsSettings`.
- Produces: update path that loads an invalid row without validating it, applies a valid replacement, validates the resulting value, and preserves normal read/error behavior when no repair is supplied.

- [ ] **Step 1: Write the failing repair test**

Insert an invalid timezone row directly, call `update_settings` with a valid
timezone and matching row version, and assert the row is repaired and audited.
Add an API test that gets a successful response for the repair.

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_repository.py tests/test_operations_api.py -k "invalid_timezone or repair" -v
```

Expected: the update path currently validates the invalid loaded value before
applying the patch and raises `invalid_operations_timezone`.

- [ ] **Step 3: Separate load validation from update validation**

Add an internal `_get_or_create_settings(..., validate_timezone=False)` call
for the update path. After applying the normalized patch, validate the
resulting timezone. If the patch does not repair the invalid value, raise the
existing stable configuration error.

- [ ] **Step 4: Run focused tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_repository.py tests/test_operations_api.py -v
```

Commit with `fix: allow repair of invalid operations timezone`.

---

### Task 6: Correct the E2E health URL fallback

**Files:**
- Create: `frontend/e2e/operations.config.ts`
- Modify: `frontend/e2e/operations.spec.ts`
- Create: `frontend/tests/operations-config.test.ts`
- Modify: `README.md`
- Modify: `docs/runbooks/operations-test-drill.md`

**Interfaces:**
- Produces: `resolveApiHealthUrl(healthUrl?: string, apiBaseUrl?: string): string` with precedence `E2E_API_HEALTH_URL`, `E2E_API_BASE_URL`, `http://127.0.0.1:8000`.

- [ ] **Step 1: Write the failing fallback test**

Test unset, empty, and explicit values. Empty strings must fall back to the
API base URL and then to `http://127.0.0.1:8000`; explicit values retain the
existing trailing-slash normalization.

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test -- operations-config.test.ts
```

Expected: the helper does not exist yet.

- [ ] **Step 3: Implement the helper and import it in the spec**

Replace the nullish-coalescing expression with the exported helper. Keep the
health request on the resolved API origin.

- [ ] **Step 4: Run focused frontend checks and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test -- operations-config.test.ts
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run typecheck
```

Commit with `fix: correct operations e2e health fallback`.

---

### Task 7: Verify the residual fix wave

**Files:**
- Modify: `.superpowers/sdd/2026-10-11-operations-safety-residuals/progress.md`
- Modify: `.superpowers/sdd/2026-10-11-operations-safety-residuals/final-report.md`
- Test: all tests changed by Tasks 1-6

**Interfaces:**
- Consumes: commits and focused tests from Tasks 1-6.
- Produces: a clean full branch review package for `670e037..HEAD` and explicit evidence for every former residual finding.

- [ ] **Step 1: Run focused backend suites**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py tests/test_operations_scheduler.py tests/test_operations_worker.py tests/test_operations_api.py tests/test_operations_domain.py tests/test_region_resolver.py tests/test_artifact_retention.py tests/test_operations_repository.py -v
```

- [ ] **Step 2: Run migration round-trip and frontend checks**

Run the migration tests, frontend unit suite, typecheck, and build. Keep the
real operations E2E explicitly skipped unless both real credentials and
`E2E_ALLOW_EVENT_PURGE=1` are available.

- [ ] **Step 3: Generate the final residual review package**

Use base `670e037` and the current head. Include the diff, commit list, test
evidence, former finding list, and all deferred Minor triage.

- [ ] **Step 4: Dispatch one independent whole-branch reviewer**

The reviewer must verify the original residual report item by item and flag
new Critical/Important issues in the residual fix diff only.

- [ ] **Step 5: Commit the final report and push the branch**

If the review is clean, commit the report and run:

```powershell
git push -u origin codex/operations-automation
```

If the review is not clean, record the findings and do not claim merge
readiness.
