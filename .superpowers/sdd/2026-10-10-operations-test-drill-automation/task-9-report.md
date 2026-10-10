# Task 9 Implementation Report

## Objective

Run the operations subsystem full verification and migration round-trip, fix the three blockers found in the first pass, record every raw command and result, and publish the final report without changing business implementation code.

## Environment Adaptations

- The existing `codex-operations` project already had healthy `postgres`, `qdrant`, and `embedding` containers.
- Host port `5432` was occupied by another project, so `POSTGRES_HOST_PORT=15433` was used.
- The frontend `run` command starts `api`; host port `8000` was occupied, so `API_PORT=18000` was used.
- No other project containers or volumes were reused or stopped.

## Fixes Applied

### QA-audit migration ownership

- Added `LATEST_REVISION = "0026_operations_automation"`.
- Renamed the head assertion to `test_qa_audit_migration_is_ancestor_of_alembic_head`.
- The test now asserts the Alembic head is `0026_operations_automation`, verifies `0025_qa_audit_provenance` resolves, and walks down-revisions to prove `0025` is an ancestor.
- The reversible-column test now restores `0026_operations_automation` in its `finally`.

### Deterministic frontend timezone

- Added `env: { TZ: "Asia/Shanghai" }` to the Vitest configuration in `frontend/vite.config.ts`.
- Extended the local configuration type with `env: Record<string, string>`.
- No command-line `TZ` override is required.

### Full backend Temporal startup documentation

- README now shows the `codex-operations` full verification sequence:
  - start `postgres temporal-postgres temporal`;
  - migrate to head;
  - run `pytest -q`.
- The text explains that complete assessment and collaboration integration tests require Temporal and warns against reusing or stopping other projects.

## Raw Commands and Results

### 1. Start Services and Migrate

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic upgrade head
```

Result: services healthy and migration complete.

### 2. Backend Full Suite

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q
```

Result:

```text
1746 passed, 3 skipped, 8 deselected, 28 warnings in 2509.70s
```

### 3. QA-audit Schema Regression

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_qa_audit_schema.py -v
```

Result:

```text
2 passed
```

After this test, `alembic current` was checked as `0026_operations_automation (head)`.

### 4. Migration and Operations Worker Round-trip

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py tests/test_operations_worker.py -v
```

Result:

```text
32 passed, 1 warning in 752.58s
```

`test_0026_operations_automation_is_reversible` verified upgrade, downgrade, re-upgrade, default settings, and cascade cleanup.

### 5. Frontend Unit Suite

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test
```

Result:

```text
Test Files  23 passed
Tests       216 passed
```

No command-line timezone override was used.

### 6. Frontend Operations E2E

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

The skip is expected because `E2E_SUPERADMIN_PASSWORD` and `E2E_ALLOW_EVENT_PURGE=1` are absent. It is not recorded as a pass.

### 7. Final Migration State

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic current
```

Result:

```text
0026_operations_automation (head)
```

## Classification

| Finding | Severity | Disposition |
| --- | --- | --- |
| Stale QA-audit head assertion | Important | Fixed; 2 focused tests pass and head restored to `0026` |
| Full backend command lacked Temporal | Important | Fixed in README; full backend now 0 failed |
| Frontend date test depended on host timezone | Minor | Fixed in Vitest config; 216 tests pass |
| Operations E2E live run | Expected limitation | Explicitly skipped, not passed |
| Shanghai MDB backend integration | Expected limitation | 3 skipped, source not installed |

## Final State

- Status: `DONE_WITH_CONCERNS`
- Final migration head: `0026_operations_automation`
- Business implementation files were not modified.
- Only the allowed test, config, README, and SDD report files were changed.
- `git diff --check` is clean before commit.
