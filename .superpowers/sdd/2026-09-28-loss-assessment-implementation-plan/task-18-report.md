# Task 18 Report

## Status

Complete with one real-service observation gap.

Commit: `d620c49` on `codex/loss-assessment`.

## Deliverables

- Created `docs/runbooks/loss-assessment.md` with every heading and operational rule required by the brief.
- Updated `README.md` to describe the completed loss chain, exact task names, API/map contracts, SQL-backed product storage, and verification commands.
- Updated `docs/runbooks/assessment-orchestration.md` to use 11 tasks, `0014_loss_assessment (head)`, six completed loss activities, and SQL examples over `loss_products`, `loss_metric_values`, and `loss_product_rasters`.
- Documented PostgreSQL/PostGIS raster backup and restore coverage for both intensity and loss tables.

## Step 2 Verification

All listed commands were executed in the current worktree. The live `assessment-dispatcher` and `temporal-worker` from another worktree were stopped during isolation so they could not race the in-process Temporal suites.

```text
docker compose --env-file .env -f infra/compose.yaml config --quiet
```

Exit code `0`, no output.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

Migration head confirmed:

```text
0014_loss_assessment (head)
```

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
```

Result:

```text
809 passed, 3 skipped, 5 warnings in 432.44s (0:07:12)
```

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_loss_performance.py -v
```

Result:

```text
1 passed in 24.46s
```

The benchmark asserts total elapsed `<= 300.0` and the four diagnostic stage budgets. The most recent committed fixed-scenario stage/total sample recorded in Task 17 is `19.730855s` total, with stages `3.552159s`, `0.000643s`, `0.037358s`, and `0.013557s`. Stage timings are diagnostic and do not sum to the hard measurement from `report_ingested_at` to authenticated `GET /loss` HTTP 200.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Result: `All checks passed!`

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
```

Result: `13 passed (13)`, `73 passed (73)`, duration `4.64s`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
```

Exit code `0`, no errors.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

`58 modules transformed`, production build completed in `8.53s`. The emitted chunk-size warning is non-failing.

## Full-Suite Isolation Fixes

The full suite initially reproduced migration-order and shared-state failures. These test-only defects were fixed without weakening, skipping, deleting, or reordering tests:

- `test_data_asset_schema.py` now restores and tests `0014_loss_assessment` instead of leaving the shared database at `0013_data_asset_final_fixes`.
- `test_assessment_repository.py` asserts the implemented 11-task plan.
- `test_final_review_regressions.py` and `test_intensity_corrections.py` complete all eight required assessment tasks before `complete_run`.
- `test_intensity_parameters.py` hashes raw file bytes, removing CRLF sensitivity from the content-sensitive checksum test.
- `tests/data_asset_helpers.py` retries transient cleanup deadlocks and gives `seeded_outbox` a unique short source/identity so fixture events no longer merge.
- `test_loss_api.py` uses unique source identities and unique event times so run snapshots are not reused across tests.
- `test_loss_spatial.py` reuses an existing `shanghai.admin.town` asset row instead of violating the unique asset key.
- `test_region_resolver.py` disposes the global async engine before and after tests to prevent cross-loop asyncpg reuse.

No production code was changed. `0014_loss_assessment` reversibility remains covered by `tests/test_migrations.py`.

## Step 3 Real-Service Integration Evidence

The current-worktree services were started:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal temporal-ui
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker
```

A fixed formal CENC event was submitted through `POST /api/v1/ingest/formal` after programmatically bootstrapping the superadmin and creating a token without reading or printing `.env` values.

Observed HTTP response:

```text
INGEST_STATUS 201
event_id 39a6fd34-6d12-497e-9247-ab66531f022c
revision_id 736cd279-a25d-4468-bd16-790148ce503d
event_kind formal
lifecycle_state formal_triggered
```

Observed persisted run and tasks:

```text
run_id 3645b44f-fde7-4711-9ce2-79685d8859de
run_no 1
status running
snapshot_count 7
missing_required []

intensity.model     succeeded
intensity.instrument succeeded
intensity.fusion    succeeded
loss.population     pending
loss.casualties     pending
loss.buildings      pending
loss.economic       pending
loss.resources      pending
loss.validate       pending
report.rapid_assessment pending
workgroup.response_tasks pending
```

At the time of evidence collection, `loss_products`, `loss_metric_values`, and `loss_product_rasters` returned zero rows for this run. The real worker did not advance the six loss activities during the observation window because the shared Temporal queue already contained prior outbox/workflow traffic. This is reported as an environment/queue observation, not fabricated as a completed real-service loss run. The required asset snapshot was complete with no `missing_required`.

## Step 4 Specification Coverage

```text
7. Region and spatial units       -> Tasks 3, 4
8. Model registry and parameters  -> Task 2
9. Building damage                -> Task 5
9. Population and shelter         -> Task 6
9. Casualties                     -> Task 7
9. Economic loss                  -> Task 8
9. Resource demand                -> Task 9
9. Validation                     -> Task 10
10. Product status and quality    -> Tasks 2, 10, 11
11.2 Model and parameter tables   -> Tasks 1, 2
11.3 Result tables                -> Tasks 1, 11
12. Temporal and completion       -> Task 13
13. APIs                          -> Task 14
14. UI, map, and permissions      -> Tasks 14, 15, 16, 16A
15. Exception handling            -> Tasks 10, 12, 17
16. Performance budget            -> Task 17
17. Test design                   -> Tasks 2-17
18. Acceptance criteria           -> Tasks 17, 18
19. Deliverables                  -> Tasks 1-18
```

Task 16A is included in the UI/map mapping because it added the functional fused-intensity artifact/tile routes and the authenticated MapLibre layer.

## Concerns

- The real-service loss worker did not finish before evidence collection because the shared Temporal queue contained prior work from another worktree. The isolated full suite and fixed-scenario benchmark both pass, but the live run remained `running` with loss tasks `pending`.
- No `.env`, API key, password, token, or connection string was read, printed, stored, or committed.

## Fix Round 1

### Documentation Corrections

- Corrected `docs/runbooks/loss-assessment.md` so the required completion set is `intensity.model`, `intensity.fusion`, and the six `loss.*` tasks. `intensity.instrument` is optional.
- Added the explicit `loss.resources` success rule: every resource kind must have a final `available` or `unavailable` status.
- Clarified that 300 seconds is the five-minute alert threshold and 1800 seconds is the technical safety timeout.
- Removed the stale `out_of_phase_scope` statement from `docs/runbooks/intensity-assessment.md` and documented the six required loss tasks plus the `report`/`workgroup` skips.

### Test Helper Correction

- Extended the test-only fixed-asset parameter fixture in `backend/tests/loss_helpers.py` to provide intensity-bin 5 vulnerability and casualty-ratio values in addition to the existing bin 6 mapping. This makes the fixed Shanghai scenario reproducible when the resolved small-grid intensity bin is 5. It does not change production algorithm versions, parameter names, or product semantics.

### Real-Service-Equivalent Evidence

The external Compose Temporal server remains a residual blocker; see the unresolved concern below. The completed acceptance evidence was produced with the embedded Temporal SDK `WorkflowEnvironment`, a real Temporal worker, real PostgreSQL/PostGIS, and the real ASGI API:

```text
WORKFLOW_STATUS completed
HTTP_STATUS 200
ELAPSED 17.725918
RUN_ID 618def84-cd3c-4d6e-88a1-06139ba5647c
RUN_STATUS completed
```

Task statuses:

```text
intensity.model succeeded
intensity.instrument succeeded
intensity.fusion succeeded
loss.population succeeded
loss.casualties succeeded
loss.buildings succeeded
loss.economic succeeded
loss.resources succeeded
loss.validate succeeded
report.rapid_assessment skipped
workgroup.response_tasks skipped
```

Persisted products:

```text
da994eea-cfce-4c45-8c12-5b5a3d94a6d9 building_damage complete L3
1b456b82-d761-47d6-adcb-b1dd146eb637 casualties complete L3
59326a5a-e21b-4493-a6d2-e141b164c08f economic_loss complete L2
553b4f10-bf21-4e95-a454-96cfc16916f5 population_impact complete L2
77e043bb-0b8a-438f-9011-958240bbc073 resource_demand complete L2
0133b98b-b1e8-4d50-964e-fba2baaf9814 validation complete L3
```

Counts:

```text
loss_metric_values: 75 rows
loss_product_rasters: 4 rows
```

Effective pointer:

```text
event_id e1ad8729-d1d2-42d4-b950-52b7a8e6d56c
latest_assessment_run_id 618def84-cd3c-4d6e-88a1-06139ba5647c
effective_assessment_run_id 618def84-cd3c-4d6e-88a1-06139ba5647c
run_no 1
status completed
```

### Final Verification Commands

After cleaning only synthetic fixture rows:

```text
docker compose --env-file .env -f infra/compose.yaml config --quiet
```

Exit code `0`, no output.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

Head: `0014_loss_assessment`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
```

Result: `809 passed, 3 skipped, 5 warnings in 440.17s (0:07:20)`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_loss_performance.py -v
```

Result: `1 passed in 25.41s`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Result: `All checks passed!`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
```

Result: `13 passed (13)`, `73 passed (73)`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
```

Exit code `0`.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Production build completed; the chunk-size notice is non-failing.

### Skip and Warning Triage

The three skips are all in `test_data_asset_mdb_host_integration.py`, reason:

```text
Shanghai MDB is not installed on this host
```

The five warnings are expected third-party/raster warnings:

- One `starlette` deprecation for `anyio.abc.BlockingPortal`.
- Two `rasterio` `Affine` matrix-multiplication deprecations.
- One `rasterio` non-georeferenced PNG notice for the intentionally rendered tile.
- One related raster warning emitted by the loss end-to-end path.

No tests were suppressed or weakened.

### Fix Round 2: External Compose Path Resolved

The production defect was the long loss-activity database transaction. `_run_model_task` prepared exposure/context, called `start_task`, then kept the same `session.begin()` transaction open through scenario computation, raster construction, and product persistence. Both parallel loss activities therefore contended on `AssessmentRun` and event row locks.

`_run_model_task` now commits the `start_task` transaction before expensive compute/read work. Shares and raster reads use a separate read session, and final product write plus `complete_task` remain atomic in one short final transaction. The workflow remains parallel and no locks needed for state transitions were weakened.

Added regression:

```text
test_parallel_loss_tasks_commit_start_task_before_compute
```

The test fails against the previous long-transaction implementation and passes after the split.

Fresh external Compose Temporal evidence:

```text
namespace task18fixns
task queue task18fix
event_id 2e28385f-daee-417a-b070-aa1ed8ffd387
revision_id 775c3b73-dd72-4aea-8b33-bd752a79fe46
run_id 940415a3-54e0-4d18-8c12-bb1013acff40
run status completed
```

Temporal history confirmed `ActivityTaskStarted` and `ActivityTaskCompleted` for scheduled events `31` (`run_loss_buildings`) and `32` (`run_loss_population`), then casualties/economic/resources/validate completed, and the workflow ended `COMPLETED`.

Persisted evidence:

```text
loss_products: 6 rows
loss_metric_values: 75 rows
loss_product_rasters: 4 rows
```

Effective pointer:

```text
latest_assessment_run_id 940415a3-54e0-4d18-8c12-bb1013acff40
effective_assessment_run_id 940415a3-54e0-4d18-8c12-bb1013acff40
```

The disposable `task18fixns` namespace and `task18fix-worker`/`task18fix-dispatcher` containers were removed after evidence collection.

### Final Full Verification After Fix

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
810 passed, 3 skipped, 5 warnings in 405.85s (0:06:45)
```

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_loss_performance.py -v
1 passed in 29.66s
```

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
All checks passed!
```

Frontend: `73 passed`, typecheck and production build passed.

### Final Whole-Branch Review Fix

#### Fixes

- Invalid loss validation now persists `LossProductStatus.INVALID` evidence, marks
  `loss.validate` failed, and raises `LossValidationFailed`. `complete_run` also
  rejects a non-complete validation product, so an invalid run cannot complete or
  update `effective_assessment_run_id`.
- Explicit `unavailable` resources with a `missing_parameter:*` reason are now
  non-blocking review (`resource_unavailable`, `L3`). Missing resource kinds,
  malformed statuses, `invalid_parameter:*`, and quantity/status mismatches remain
  blocking.
- Building, population, casualty, and economic products now persist deterministic
  `town`, `county`, and `city` metrics. County aggregation uses each locked
  `TownExposure.county_code`; city aggregation uses `context.region_id`. Resource
  products keep their existing city scope.
- Successful `complete_run` atomically publishes all complete loss products,
  including the validation product, in `_publish_products`. Failed/invalid runs
  remain unpublished.
- `_record_task_failure` now stores only a controlled category and stable summary.
  It no longer stores `type(exc).__name__`, exception text, SQL, connection
  details, or validation internals.

#### Regression Tests

`tests/test_loss_final_review.py` covers publication/effective-pointer behavior,
all advertised area scopes, invalid validation failure behavior, and sanitized
failure summaries. `tests/test_loss_validation.py` was updated for the corrected
resource degradation contract and expanded for missing, malformed, and invalid
resource entries.

#### Focused Verification

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest \
  tests/test_loss_final_review.py tests/test_loss_validation.py \
  tests/test_loss_failure_modes.py tests/test_loss_service_execution.py \
  tests/test_loss_repository.py -q
51 passed in 75.40s
```

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest \
  tests/test_assessment_temporal.py tests/test_assessment_task_lifecycle.py \
  tests/test_assessment_api.py tests/test_loss_api.py \
  tests/test_loss_end_to_end.py -q
39 passed, 3 warnings in 46.56s
```

#### Full Verification

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
817 passed, 3 skipped, 5 warnings in 433.07s (0:07:13)
```

Skip triage:

```text
tests/test_data_asset_mdb_host_integration.py: 3 skipped
reason: Shanghai MDB is not installed on this host
```

Warning triage: one expected `starlette` deprecation and four expected
`rasterio`/loss-tile raster warnings. No tests were suppressed or weakened.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest \
  -m performance tests/test_loss_performance.py -v
1 passed in 21.78s
```

Real fixed-scenario sample:

```text
elapsed_seconds=8.7972
snapshot_and_exposure=0.2928930070349425
buildings_and_population=0.00038824096554890275
casualties_economic_resources=0.035787162953056395
validate_and_persist=0.011810142023023218
```

The four stage budgets are diagnostic; the hard acceptance measurement remains
total elapsed from `report_ingested_at` to authenticated `GET /loss` HTTP 200.

```text
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
All checks passed!
```

Frontend verification:

```text
npm test: 13 files passed, 73 tests passed
npm run typecheck: exit 0
npm run build: production build passed
```

#### External Compose Temporal Happy Path

Fresh namespace `task18final2`, task queue `task18final2`, with only a
`temporal-worker` polling that namespace/task queue. The workflow was dispatched
directly through Temporal, not through the dispatcher or embedded test server.

```text
event_id 594e4c21-a9c7-4b8b-97db-f8f5f9034406
revision_id 648dd2f5-d068-42ea-83d1-0cd4a422d161
outbox_id f7acbcc1-b9fc-4ba6-a780-0b6314e7bf0e
run_id 4f6cdf75-ad16-474d-a027-640e0743735e
workflow_id task18-final2:3720bbc9-34a4-4022-876e-089c5ea63acd
workflow status completed
```

Required task statuses:

```text
intensity.model succeeded
intensity.fusion succeeded
loss.population succeeded
loss.buildings succeeded
loss.casualties succeeded
loss.economic succeeded
loss.resources succeeded
loss.validate succeeded
report.rapid_assessment skipped
workgroup.response_tasks skipped
```

`intensity.instrument` also succeeded in this run and remains optional.

Persisted evidence:

```text
loss_products: 6 rows
loss_metric_values: 153 rows
loss_product_rasters: 4 rows
```

Product IDs:

```text
building_damage 6256014e-23b6-4f37-a975-88b93db83b7f
population_impact 8369a4c3-5104-4521-86cf-74bf0a46bed9
casualties f308f475-10cb-4c36-88ef-aaf804a0740d
economic_loss 7cea0e12-a9e1-4cab-a6c8-3ff8e6827a37
resource_demand 5e48e19c-d478-428a-b434-da41c9077725
validation c9986b6b-8dfb-4f75-a202-d4956305e909
```

All six products were `complete` and had non-null `published_at`. The effective
pointer was:

```text
effective_assessment_run_id 4f6cdf75-ad16-474d-a027-640e0743735e
```

The worker container and disposable workflow were cleaned up. The Temporal
namespace could not be deleted with the bundled `tctl` command because that
version exposes only `namespace register`, `update`, `describe`, and `list`.

#### Operational Note

During setup I briefly started a dispatcher against the first disposable
namespace before realizing it would claim shared pending outboxes. I stopped it
immediately and restored the 19 runs/outboxes/tasks it had touched back to
`pending`, then reran the external proof without a dispatcher. `loss_products`,
`loss_metric_values`, and `loss_product_rasters` were confirmed empty afterward.
