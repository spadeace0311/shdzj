# Task 9 Implementation Report

## Objective

Run the operations subsystem full verification and migration round-trip, record every raw command and its real result, and publish the final report without modifying application code.

## Environment Adaptations

- The existing `codex-operations` project already had healthy `postgres`, `qdrant`, and `embedding` containers.
- The first exact backend command recreated `codex-operations-postgres-1` and collided with host port `5432`.
- All reruns stayed in the `codex-operations` project and used `POSTGRES_HOST_PORT=15433`.
- The frontend `run` command starts `api` as a dependency; host port `8000` was occupied by another project, so `API_PORT=18000` was used.
- No other project containers or volumes were reused or stopped.

## Raw Commands and Results

### 1. Backend Full Suite

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q
```

Result:

```text
4 failed, 1742 passed, 3 skipped, 8 deselected, 26 warnings in 2336.35s
```

Failures:

```text
tests/test_assessment_temporal.py::test_temporal_delayed_assessment_does_not_launch_stale_artifact_child
tests/test_collaboration_end_to_end.py::test_formal_event_closes_all_seven_groups_with_dual_versions
tests/test_collaboration_end_to_end.py::test_correction_keeps_frozen_version_and_drill_flows_through_worker
tests/test_qa_audit_schema.py::test_qa_audit_migration_is_alembic_head
```

The three Temporal failures were caused by `temporal` not being a dependency of `api`; the command did not start it. The QA-audit failure is a stale assertion for `0025_qa_audit_provenance`.

### 2. Backend Skip Identification

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_data_asset_mdb_host_integration.py -q -rs
```

Result:

```text
3 skipped
SKIPPED [1] ... Shanghai MDB is not installed on this host
SKIPPED [1] ... Shanghai MDB is not installed on this host
SKIPPED [1] ... Shanghai MDB is not installed on this host
```

### 3. Temporal Availability Recheck

Command:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d temporal-postgres temporal
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q tests/test_assessment_temporal.py::test_temporal_delayed_assessment_does_not_launch_stale_artifact_child tests/test_collaboration_end_to_end.py::test_formal_event_closes_all_seven_groups_with_dual_versions tests/test_collaboration_end_to_end.py::test_correction_keeps_frozen_version_and_drill_flows_through_worker
```

Result:

```text
3 passed in 225.18s
```

This recheck ran only after the migration round-trip restored the database to `0026_operations_automation`. It confirms the three full-suite failures are environmental, not application regressions.

### 4. Migration Upgrade/Downgrade/Re-Upgrade

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py tests/test_operations_worker.py -v
```

Result:

```text
32 passed, 1 warning in 752.21s
```

`test_0026_operations_automation_is_reversible` verified:

- `0025 -> 0026` upgrade creates the four operations tables and default settings.
- `0026 -> 0025` downgrade removes the tables.
- `0025 -> 0026` re-upgrade restores tables and exactly one default settings row.
- Deleting an operations test/drill event cascades to `operations_test_schedules` and `operations_runs`.

### 5. Frontend Unit Suite, Default Timezone

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test
```

Result:

```text
Test Files  1 failed | 22 passed
Tests       1 failed | 215 passed
```

Failure:

```text
workgroup-tasks.test.tsx:720 expected due_at "2026-10-03T02:00:00.000Z"
received due_at "2026-10-03T10:00:00.000Z"
```

The input `2026-10-03T10:00` is treated as UTC in the default container timezone.

### 6. Frontend Unit Suite, Shanghai Timezone

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm -e TZ=Asia/Shanghai frontend npm test
```

Result:

```text
Test Files  23 passed
Tests       216 passed
```

This confirms the default-timezone failure is a test-portability issue.

### 7. Frontend Operations E2E

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run test:e2e -- operations.spec.ts
```

Result:

```text
1 skipped
```

The skip requires either `E2E_SUPERADMIN_PASSWORD` or `E2E_ALLOW_EVENT_PURGE=1`; both are absent. It was not recorded as a pass.

### 8. Final Migration State

Command:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic current
```

Result:

```text
0026_operations_automation (head)
```

## Classification

| Finding | Severity | Type | Verified disposition |
| --- | --- | --- | --- |
| Three Temporal integration failures in Step 1 | Important | test harness/environment | All 3 pass after starting `temporal` and restoring head |
| Stale QA-audit Alembic head assertion | Important | test-suite regression | Still failing; leaves DB at `0025` |
| Frontend due-date timezone assertion | Minor | test portability | Passes with `TZ=Asia/Shanghai`; fails default container |
| Operations E2E live run | Expected limitation | missing credentials/purge gate | Explicitly skipped, not passed |
| Shanghai MDB backend integration | Expected limitation | host-only data missing | 3 skipped |

## Final State

- Status: `BLOCKED`
- HEAD before Task 9: `909d110`
- Task 9 modifies only `.superpowers/sdd/2026-10-10-operations-test-drill-automation`
- No application source files changed.
- `git diff --check` was run before commit and was clean.
