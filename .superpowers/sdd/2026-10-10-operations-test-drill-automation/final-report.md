# Operations Test/Drill Automation - Final Verification Report

## Status

- Plan: `docs/superpowers/plans/2026-10-10-operations-test-drill-automation.md`
- Branch: `codex/operations-automation`
- Reviewed range: `5a5dcaf..909d110`
- Final verification status: `BLOCKED`

The subsystem implementation and its focused reviews are complete, but the final full-suite verification is not fully green. Two test-suite defects and one command/dependency gap must be resolved before the acceptance checklist can be marked fully satisfied.

## Commit Scope

- HEAD: `909d110`
- Commit count: 24
- Diff summary: `44 files changed, 9413 insertions(+), 14 deletions(-)`
- Main surfaces:
  - Backend operations persistence, migration `0026_operations_automation`, domain, repository, priority guard, trigger API, scheduler, worker, health.
  - Frontend operations console, client, route, page, styles, page tests.
  - E2E `frontend/e2e/operations.spec.ts`.
  - Runbook `docs/runbooks/operations-test-drill.md`.
  - Compose `operations-worker` service and `.env.example` documentation.
- Task 9 changes only documentation under `.superpowers/sdd/2026-10-10-operations-test-drill-automation`; it does not modify application code.

## Commands Executed

The exact brief command first collided with host port `5432`, so the same `codex-operations` project was rerun with `POSTGRES_HOST_PORT=15433` and, for frontend dependency startup, `API_PORT=18000`.

Backend full suite:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q
```

Result:

```text
4 failed, 1742 passed, 3 skipped, 8 deselected in 2336.35s
```

Temporal availability verification:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d temporal-postgres temporal
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q tests/test_assessment_temporal.py::test_temporal_delayed_assessment_does_not_launch_stale_artifact_child tests/test_collaboration_end_to_end.py::test_formal_event_closes_all_seven_groups_with_dual_versions tests/test_collaboration_end_to_end.py::test_correction_keeps_frozen_version_and_drill_flows_through_worker
```

Result: `3 passed`.

Migration round-trip:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py tests/test_operations_worker.py -v
```

Result: `32 passed`.

Frontend unit suite, default container timezone:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test
```

Result: `1 failed, 215 passed`.

Frontend unit suite, Shanghai timezone:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm -e TZ=Asia/Shanghai frontend npm test
```

Result: `216 passed`.

Frontend operations E2E:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run test:e2e -- operations.spec.ts
```

Result: `1 skipped`.

## Pass Counts

| Suite | Passed | Failed | Skipped | Deselected |
| --- | ---: | ---: | ---: | ---: |
| Backend full | 1742 | 4 | 3 | 8 |
| Backend Temporal recheck | 3 | 0 | 0 | 0 |
| Migration and operations worker | 32 | 0 | 0 | 0 |
| Frontend default TZ | 215 | 1 | 0 | 0 |
| Frontend Asia/Shanghai | 216 | 0 | 0 | 0 |
| Frontend operations E2E | 0 | 0 | 1 | 0 |

The `8 deselected` backend items are the repository-wide performance marker exclusion from `pyproject.toml` (`-m 'not performance'`).

## Skip Reasons

- `tests/test_data_asset_mdb_host_integration.py`: `3 skipped`, reason `Shanghai MDB is not installed on this host`.
- `frontend/e2e/operations.spec.ts`: `1 skipped` because `E2E_SUPERADMIN_PASSWORD` is not set and `E2E_ALLOW_EVENT_PURGE=1` is not set.
- The operations E2E skip is intentional and must not be recorded as a pass.

## E2E Evidence

- Source: [operations.spec.ts](C:/Users/User/.codex/worktrees/map-report-merge-main/%E5%9C%B0%E9%9C%87%E5%BA%94%E6%80%A5%E8%BE%85%E5%8A%A9%E5%86%B3%E7%AD%96%E7%B3%BB%E7%BB%9F/frontend/e2e/operations.spec.ts)
- The test performs a top-level `test.skip` unless both the real superadmin password and the explicit purge gate are present.
- The live scenario covers manual test trigger, complete assessment, 39-output artifact production, 27 map artifacts, 12 report/deck artifacts, test markers, real-event priority 409, and test retention age.
- Actual Task 9 run: `1 skipped`. No live E2E pass can be claimed without the two required environment variables.

## Residual Risks

### Important - stale QA-audit migration test

`backend/tests/test_qa_audit_schema.py` still asserts `AUDIT_REVISION = "0025_qa_audit_provenance"` and restores `0025` in its `finally` block. After Task 1 made `0026_operations_automation` the Alembic head, this test:

- fails `test_qa_audit_migration_is_alembic_head`;
- leaves the database at `0025` after a full run, which makes later operations-dependent tests fail until the head is restored.

This is a test-suite regression, not an application runtime defect. It should be fixed in the QA-audit test ownership before merge.

### Important - full backend command does not start Temporal

The brief Step 1 command uses the `api` service. `api` does not depend on `temporal`, so three real Temporal integration tests fail with DNS resolution. Starting `codex-operations` `temporal-postgres` and `temporal` makes all three pass. The runbook or brief command needs to include Temporal for a meaningful full backend run.

### Minor - timezone-portable frontend unit test

`frontend/tests/workgroup-tasks.test.tsx` expects `"2026-10-03T10:00"` to serialize as `2026-10-03T02:00:00.000Z`. This is only true when the test process timezone is `Asia/Shanghai`. The default container run failed `1` test; rerunning with `TZ=Asia/Shanghai` passed `216`. The test should set `TZ=Asia/Shanghai` or parse the input with an explicit timezone.

### Deferred Minors

All deferred Minors and rulings recorded in `progress.md` remain open and are not deleted. They include:

- Task 1: duplicated `OPERATIONS_TABLES` constant.
- Task 2: deterministic barrier and CPython random-sequence portability.
- Task 3: broad exception mapping, duplicate audit row, incomplete positive permission coverage.
- Task 4: disabled schedule counted as due, weak variation assertion.
- Task 5: timezone validation, session-protocol boundary, additional 409 coverage.
- Task 6: logging level, deployment health semantics, Compose healthcheck polish, upstream Starlette warning.
- Task 7: permission affordance for trigger buttons.
- Task 8: E2E health URL, runbook active-real-event precision, pause wording.

## Merge Recommendation for Deferred Minors

- The deferred Minors can remain as non-blocking follow-up items and should be tracked in the next hardening backlog.
- The newly discovered Important items above should be fixed before merge because they prevent a clean full-suite verification and can leave the shared test database at an unexpected revision.
- The E2E live run remains pending real credentials and purge authorization; merge can proceed with the test marked as explicitly skipped, provided this limitation is retained in the report.

## Final Acceptance Checklist

| Checklist item | Conclusion |
| --- | --- |
| 每日测试在上海时区配置窗口内随机触发一次 | PASS by Task 2/4 unit tests; not re-verified live because E2E skipped |
| 同一本地日期不会重复创建或触发测试 | PASS by unique constraint and Task 2/4 tests |
| 手工测试、演练和手工正式事件入口彼此独立 | PASS by Task 3/5 API tests and Task 7 page tests |
| 上海行政区内 `5.0-5.9` 测试事件创建完整评估和成果生产 | PASS by focused service/artifact tests; live E2E skipped |
| 真实事件活动期间测试延后，非正式成果遵守优先终止规则 | PASS by Task 5 tests and E2E source; live E2E skipped |
| 演练冲突策略支持暂停和终止 | PASS by Task 5 unit tests |
| 冻结或取消的测试/演练不能继续提交成果或产生截止提醒 | PASS by Task 5 tests |
| 测试成果保留 400 天，演练成果保留两年 | PASS by Task 6 retention tests; live E2E skipped |
| 测试和演练标识不可移除 | PASS by naming/marker unit tests; live E2E skipped |
| 运维 API、页面、worker、健康检查和审计记录完整 | PASS by focused tests and 1742 backend tests |
| 迁移、后端、前端、E2E、Ruff、类型检查、构建和 Compose 校验有真实证据 | NOT SATISFIED: backend full run has 4 failures, frontend default run has 1 failure, operations E2E is skipped |
