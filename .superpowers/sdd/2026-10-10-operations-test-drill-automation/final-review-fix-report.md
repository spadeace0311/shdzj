# Final Review Fix Report

## Summary

- Branch: `codex/operations-automation`
- Repair base: `c66e2d051dcb877c24aa999d30b996a1cd24da25`
- Implementation commit: `b3ff90f`
- Final status: `DONE_WITH_CONCERNS`
- Migration head: `0027_operations_safety_hardening`
- Live operations E2E: explicitly skipped, not reported as passed

## Verification Evidence

### Passing Focused Backend Suites

```text
301 passed, 15 warnings
```

The passing focused command covered:

```text
tests/test_operations_domain.py
tests/test_operations_repository.py
tests/test_operations_service.py
tests/test_operations_scheduler.py
tests/test_operations_worker.py
tests/test_operations_priority.py
tests/test_operations_api.py
tests/test_event_api.py
tests/test_event_service.py
tests/test_event_service_responses.py
tests/test_event_schema.py
tests/test_region_resolver.py
tests/test_collaboration_tasks_api.py
tests/test_collaboration_notifications.py
tests/test_collaboration_deliverables_api.py
tests/test_collaboration_retention.py
tests/test_artifact_api.py
tests/test_artifact_naming.py
tests/test_artifact_override.py
tests/test_artifact_retention.py
tests/test_artifact_cancellation.py
tests/test_operations_schema.py
selected migration tests
```

The first passing focused subset after the initial triage was `72 passed`; the final broad focused run is the authoritative focused result: `301 passed`.

### Additional Focused Regression After Late Edits

```text
75 passed, 1 warning
```

This run covered the final worker retention integration, artifact override guards, collaboration marker routes, timezone API validation, QA audit migration head, artifact API and operations API regressions.

### Migration Round Trip

```text
4 passed
```

Covered:

- Alembic head equals `0027_operations_safety_hardening`.
- `0026_operations_automation` remains a valid previous operations revision.
- `0027_operations_safety_hardening` upgrades, downgrades, and re-upgrades.
- Operations tables, constraints, and default settings survive the round trip.

### Affected Temporal Integration Rerun

```text
3 passed
```

Covered:

```text
test_assessment_temporal.py::test_temporal_delayed_assessment_does_not_launch_stale_artifact_child
test_collaboration_end_to_end.py::test_formal_event_closes_all_seven_groups_with_dual_versions
test_collaboration_end_to_end.py::test_correction_keeps_frozen_version_and_drill_flows_through_worker
```

### QA, Spatial, Roster and Collaboration Affected Cases

```text
17 passed
```

The `test_qa_audit_schema.py`, `test_qa_spatial_tools.py`, `test_workgroup_roster.py`, and the two collaboration end-to-end cases were rerun together. The three temporal-dependent failures in that run were then fixed by the isolated Temporal environment and passed in the rerun above.

### Frontend

```text
23 passed
```

Focused Operations and manual-event page tests.

```text
218 passed
```

Full frontend Vitest suite.

```text
npm run typecheck: passed
npm run build: passed
```

The build retains the existing Vite large-chunk warning, which is non-blocking and pre-existing.

### Static and Compose Checks

```text
ruff check app tests: passed
git diff --check: passed
docker compose ... config --quiet: passed with the temporary isolated test override
```

The temporary override was removed before the final report commit and is not present in Git.

### Live Operations E2E

```text
1 skipped
```

Reason:

- `E2E_SUPERADMIN_PASSWORD` is not available.
- `E2E_ALLOW_EVENT_PURGE=1` is not set.

The test remained a real skip and was never converted to a pass. The E2E health URL now targets the API health origin rather than the frontend origin.

## Item Results

Every item is implemented in commit `b3ff90f`.

| Item | Result | Key Files |
| --- | --- | --- |
| Critical 1: legacy `manual\|test\|drill` bypass closed | DONE | `backend/app/events/schemas.py`, `frontend/src/pages/ManualEventPage.tsx`, `backend/tests/test_event_api.py`, `backend/tests/test_operations_priority.py` |
| Critical 2: correction remains a real priority state | DONE | `backend/app/events/service.py`, `backend/app/operations/priority.py`, `backend/tests/test_operations_priority.py` |
| Critical 3: non-removable markers across collaborative and artifact override paths | DONE | `backend/app/operations/markers.py`, `backend/app/collaboration/service.py`, `backend/app/collaboration/models.py`, `backend/app/artifacts/service.py`, `backend/tests/test_collaboration_deliverables_api.py`, `backend/tests/test_artifact_override.py` |
| Important 1: freeze coverage, run reconciliation, authorized drill recovery | DONE | `backend/app/collaboration/service.py`, `backend/app/collaboration/scheduler.py`, `backend/app/operations/service.py`, `backend/app/operations/router.py`, `backend/app/operations/worker.py`, `backend/tests/test_operations_priority.py`, `backend/tests/test_operations_service.py` |
| Important 2: atomic boundary generation and `ST_Covers` | DONE | `backend/app/operations/scenario.py`, `backend/app/operations/domain.py`, `backend/app/regions/importer.py`, `backend/app/regions/service.py`, `backend/app/operations/service.py`, `backend/tests/test_operations_domain.py`, `backend/tests/test_region_resolver.py` |
| Important 3: timezone validation and ingestion safety | DONE | `backend/app/operations/repository.py`, `backend/app/operations/schemas.py`, `backend/app/operations/priority.py`, `backend/app/operations/router.py`, `backend/tests/test_operations_repository.py`, `backend/tests/test_operations_api.py` |
| Important 4: unified retention, active production protection, reentrant cleanup | DONE | `backend/app/operations/retention_clock.py`, `backend/app/collaboration/retention.py`, `backend/app/artifacts/retention.py`, `backend/app/artifacts/worker.py`, `backend/app/operations/worker.py`, `backend/tests/test_collaboration_retention.py`, `backend/tests/test_operations_worker.py` |
| Important 5: reserved scheduler idempotency namespace | DONE | `backend/app/operations/service.py`, `backend/tests/test_operations_service.py` |
| Important 6: priority transition and release audit rows | DONE | `backend/app/operations/priority.py`, `backend/tests/test_operations_priority.py` |
| Important 7: artifact override freeze and active-real rejection | DONE | `backend/app/artifacts/service.py`, `backend/app/artifacts/router.py`, `backend/tests/test_artifact_override.py`, `backend/tests/test_artifact_api.py` |
| Important 8: non-backfillable real-event activation time | DONE | `backend/app/events/models.py`, `backend/app/events/repository.py`, `backend/app/operations/priority.py`, `backend/migrations/versions/0027_operations_safety_hardening.py`, `backend/tests/test_operations_priority.py` |
| Important 9: E2E health precision and explicit skip | DONE_WITH_CONCERNS | `frontend/e2e/operations.spec.ts`, `README.md`, `docs/runbooks/operations-test-drill.md` |
| Important 10: heartbeat-backed Compose healthcheck | DONE | `backend/app/operations/health.py`, `backend/app/operations/health_check.py`, `backend/app/operations/worker.py`, `infra/compose.yaml`, `backend/tests/test_operations_worker.py` |

Deferred blockers called out by the review were handled with the corresponding Important items:

- Timezone validation is Important 3.
- Direct upload/override/restore freeze coverage is Important 1 and Critical 3.
- The seven-day `origin_time` policy is replaced by Important 8.
- Run-state reconciliation is Important 1.
- Deployment health is Important 10.
- E2E health URL precision is Important 9.

## Migration

New migration:

```text
backend/migrations/versions/0027_operations_safety_hardening.py
```

The migration adds:

- `earthquake_events.real_event_activated_at`
- `earthquake_events.real_event_closed_at`
- indexes for activation and closure timestamps
- `collaboration_deliverable_versions.operation_marker`
- `collaboration_deliverable_publications.operation_marker`
- database check constraints limiting markers to `test|drill`

It does not alter migration `0026` or earlier semantics.

## First Full Backend Attempt

The first full backend attempt produced:

```text
1759 passed, 3 skipped, 8 deselected, 13 failed, 4 errors
```

The failures were accounted for as follows:

- `test_qa_audit_schema.py` still expected `0026`; fixed to `0027`.
- The existing Temporal PostgreSQL volume used incompatible authentication; replaced with a fresh project-scoped test volume.
- Migration test ordering briefly left the shared schema below the new head; this was corrected by the QA audit head update and round-trip coverage.
- All non-temporal affected QA, spatial, roster, and collaboration cases passed in a focused rerun.
- All three affected Temporal cases passed after the isolated Temporal environment was fixed.

The second full-suite rerun was stopped by the user before it reached a final result. This report therefore does not claim a fresh full-suite pass. The stable evidence is:

- broad focused backend: `301 passed`
- late focused regressions: `75 passed`
- migration round trip: `4 passed`
- affected Temporal: `3 passed`
- affected QA/spatial/roster/collaboration: `17 passed`
- frontend full: `218 passed`

## External Verification Limitation

The live operations E2E remains externally blocked:

```text
E2E_SUPERADMIN_PASSWORD missing
E2E_ALLOW_EVENT_PURGE=1 missing
```

No credential, password, token, API key, connection string, `.env` content, or real credential was read, printed, staged, or committed.

## Temporary Untracked File

`infra/codex-test.override.yaml` was used only to run tests in the isolated `codex-operations` project without a local `.env`. It pointed at placeholder test credentials and fresh project-scoped PostgreSQL/Temporal volumes. It was removed after testing and is not in commit `b3ff90f`.

## Retained Non-Blocking Minors

The previously classified non-blocking Minors remain unchanged, including:

- broad unknown-exception trigger mapping outside the timezone path
- duplicate idempotent trigger replay audit row
- trigger controls visible to roles receiving 403
- documented bearer-token requirement
- existing timing/portability test risks
- `OperationsTestSchedule.scenario` not populated in all backend paths

No unrelated refactor was performed.

## Final State

`DONE_WITH_CONCERNS`

Concerns:

1. Live operations E2E remains explicitly skipped because the required real password and purge authorization were absent.
2. The second full backend rerun was interrupted by the user before completion; the broad focused and affected-test evidence is green.
3. The three existing MDB host-integration skips remain external environment skips.
