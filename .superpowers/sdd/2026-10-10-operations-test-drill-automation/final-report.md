# Operations Test/Drill Automation - Final Verification Report

## Status

- Plan: `docs/superpowers/plans/2026-10-10-operations-test-drill-automation.md`
- Branch: `codex/operations-automation`
- Reviewed implementation range: `6d55b8e..89cec18`
- Final implementation head: `b3ff90f`
- Final review/report head: `89cec18`
- Final verification status: `NOT MERGE-READY`

The pre-hardening full backend suite, migration round-trip, operations worker tests, and frontend unit suite were green. The final whole-branch review then found safety issues; one concentrated fix wave closed all three original Critical findings, but the single scoped re-review still found one new Critical, two new Important issues, four original Important findings not fully addressed, and the externally blocked live E2E. The branch is therefore suitable for remote backup and continued work on `codex/operations-automation`, but it is not ready to merge into `main`.

This report's later command tables and acceptance checklist preserve the earlier `c66e2d0` verification record for traceability. They are not the final merge verdict. The authoritative post-review outcome is in "Final Whole-Branch Review Outcome" below.

## Commit Scope

- Final range contains 29 commits and `74 files changed, 13935 insertions(+), 64 deletions(-)`.
- Verification fixes in this task cover:
  - `backend/tests/test_qa_audit_schema.py`: QA-audit migration is now verified as an ancestor of the current Alembic head and restores `0026_operations_automation`.
  - `frontend/vite.config.ts`: Vitest is pinned to `TZ=Asia/Shanghai` so date tests do not depend on the host timezone.
  - `README.md`: full backend verification now documents starting `postgres temporal-postgres temporal` in `codex-operations` before `pytest`.
  - SDD verification reports and ledger.
  - `b3ff90f`: one final-review fix wave covering legacy-entry protection, correction priority, marker enforcement, retention, boundary validation, health checks, and audit behavior.
  - `0027_operations_safety_hardening`: migration for activation/closure timestamps and immutable operation-marker metadata.

## Final Whole-Branch Review Outcome

- Original review: `3 Critical`, `10 Important`; original review report: `final-review-c66e2d0.md`.
- Concentrated fix: `b3ff90f`; fix report: `final-review-fix-report.md`.
- Scoped re-review: `1 Critical`, `2 Important` new; `4` original Important incomplete; live E2E still skipped; report: `re-review-89cec18.md`.
- All three original Critical findings are addressed: legacy test/drill API bypass, correction dropping real-event priority, and removable collaborative/artifact markers.
- The fix wave introduced or exposed a destructive scheduled-key upgrade path: a crash between run creation and schedule marking, followed by upgrade to the renamed scheduler key, can delete the canonical event instead of recovering it (`backend/app/operations/service.py:110`, `backend/app/operations/scheduler.py:218-274`, `backend/app/operations/service.py:691-730`).
- Run reconciliation/resume remains incomplete: missing production rows can be marked completed, and repeated resume can create duplicate replacement drills without closing the source run (`backend/app/operations/service.py:295-429`).
- Boundary generation is still split across transactions: a concurrent boundary activation can make the generated point resolve against a different active version (`backend/app/operations/service.py:488-518`, `backend/app/regions/service.py:77-81`, `backend/app/events/service.py:347-364`).
- Artifact retention can still delete generated artifacts that belong to an active mixed production run because it does not exclude active `ProductionRun`/`ProductionTask` state (`backend/app/artifacts/retention.py:50-70`, `backend/app/operations/worker.py:139-150`).
- `artifact_retention_enabled=false` is no longer honored by the integrated operations worker path (`backend/app/operations/worker.py:123-150`).
- A pre-existing invalid timezone row cannot be repaired through the API because reads validate before applying the replacement patch (`backend/app/operations/repository.py:97`, `326-332`).
- The E2E health URL fallback is still incorrect when `E2E_API_BASE_URL` is absent (`frontend/e2e/operations.spec.ts:4-9`), and the live operations E2E remains `1 skipped` because the real superadmin password and purge authorization are absent.
- Post-fix evidence is focused rather than a complete backend rerun: `301 passed`, then `75 passed`; migration round-trip `4 passed`; affected Temporal `3 passed`; frontend `218 passed`; Ruff, typecheck, build, and Compose config passed. The post-fix full backend suite does not have a completed green result.

## Final Adjudication

- Ruling: do not merge this branch to `main` while the residual Critical/Important findings above remain open. Cost if wrong: the next development round must remediate issues that could otherwise have been integrated immediately.
- Ruling: push the feature branch to `origin/codex/operations-automation` as a recoverable checkpoint because the user explicitly requested GitHub push and the worktree is clean. Cost if wrong: the remote contains a feature branch that reviewers must not merge until the residual findings are closed.
- Ruling: preserve the live E2E skip as an external verification limitation rather than fabricate credentials or mark it as a pass. Cost if wrong: the end-to-end 39-artifact workflow remains unverified on this host.

## Commands Executed

All commands use the isolated Compose project `codex-operations`. Host port `5432` was occupied, so `POSTGRES_HOST_PORT=15433` was used.

Start required services and migrate:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic upgrade head
```

Backend full suite:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q
```

Result:

```text
1746 passed, 3 skipped, 8 deselected, 28 warnings in 2509.70s
```

QA-audit schema focused regression:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_qa_audit_schema.py -v
```

Result: `2 passed`.

Migration and operations worker round-trip:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py tests/test_operations_worker.py -v
```

Result: `32 passed`.

Frontend unit suite:

```powershell
$env:POSTGRES_HOST_PORT='15433'
$env:API_BIND_HOST='127.0.0.1'
$env:API_PORT='18000'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test
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

Final migration state:

```powershell
$env:POSTGRES_HOST_PORT='15433'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic current
```

Result: `0026_operations_automation (head)`.

## Pass Counts

| Suite | Passed | Failed | Skipped | Deselected |
| --- | ---: | ---: | ---: | ---: |
| Backend full | 1746 | 0 | 3 | 8 |
| QA-audit schema | 2 | 0 | 0 | 0 |
| Migration and operations worker | 32 | 0 | 0 | 0 |
| Frontend unit | 216 | 0 | 0 | 0 |
| Frontend operations E2E | 0 | 0 | 1 | 0 |

The `8 deselected` backend items are the repository-wide performance marker exclusion from `pyproject.toml` (`-m 'not performance'`).

## Skip Reasons

- `tests/test_data_asset_mdb_host_integration.py`: `3 skipped`, reason `Shanghai MDB is not installed on this host`.
- `frontend/e2e/operations.spec.ts`: `1 skipped` because `E2E_SUPERADMIN_PASSWORD` is not set and `E2E_ALLOW_EVENT_PURGE=1` is not set.
- The operations E2E skip is intentional and is not recorded as a pass.

## E2E Evidence

- Source: [operations.spec.ts](C:/Users/User/.codex/worktrees/map-report-merge-main/%E5%9C%B0%E9%9C%87%E5%BA%94%E6%80%A5%E8%BE%85%E5%8A%A9%E5%86%B3%E7%AD%96%E7%B3%BB%E7%BB%9F/frontend/e2e/operations.spec.ts)
- The test performs a top-level `test.skip` unless both the real superadmin password and the explicit purge gate are present.
- The live scenario covers manual test trigger, complete assessment, 39-output artifact production, 27 map artifacts, 12 report/deck artifacts, test markers, real-event priority 409, and test retention age.
- Actual Task 9 run: `1 skipped`. No live E2E pass is claimed without the two required environment variables.

## Fixed Blockers

- QA-audit migration test now asserts current head is `0026_operations_automation`, verifies `0025_qa_audit_provenance` resolves and is an ancestor, and restores the latest head in `finally`.
- Vitest now sets `TZ=Asia/Shanghai` through `vite.config.ts`, so frontend tests pass without command-line timezone overrides.
- README now documents starting `postgres temporal-postgres temporal` before full backend verification because complete assessment and collaboration integration tests require Temporal.

## Residual Risks

- The live operations E2E remains unexecuted pending `E2E_SUPERADMIN_PASSWORD` and `E2E_ALLOW_EVENT_PURGE=1`.
- `tests/test_data_asset_mdb_host_integration.py` remains skipped on this host because the Shanghai MDB source is not installed.
- All deferred Minors and rulings recorded in `progress.md` remain open.

## Deferred Minors

The deferred Minors from Tasks 1-8 are preserved and include:

- Task 1: duplicated `OPERATIONS_TABLES` constant.
- Task 2: deterministic barrier and CPython random-sequence portability.
- Task 3: broad exception mapping, duplicate audit row, incomplete positive permission coverage.
- Task 4: disabled schedule counted as due, weak variation assertion.
- Task 5: timezone validation, session-protocol boundary, additional 409 coverage.
- Task 6: logging level, deployment health semantics, Compose healthcheck polish, upstream Starlette warning.
- Task 7: permission affordance for trigger buttons.
- Task 8: E2E health URL, runbook active-real-event precision, pause wording.

## Merge Recommendation for Deferred Minors

- The deferred Minors are non-blocking and should be tracked in the next hardening backlog.
- The newly discovered blockers from the first verification pass are fixed and no longer block merge.
- The live E2E limitation and Shanghai MDB skip must remain documented; the operations E2E must stay explicitly skipped until real credentials and purge authorization are supplied.

## Final Acceptance Checklist

| Checklist item | Conclusion |
| --- | --- |
| 每日测试在上海时区配置窗口内随机触发一次 | PASS by Task 2/4 unit tests; live E2E remains skipped |
| 同一本地日期不会重复创建或触发测试 | PASS by unique constraint and Task 2/4 tests |
| 手工测试、演练和手工正式事件入口彼此独立 | PASS by Task 3/5 API tests and Task 7 page tests |
| 上海行政区内 `5.0-5.9` 测试事件创建完整评估和成果生产 | PASS by focused service/artifact tests; live E2E remains skipped |
| 真实事件活动期间测试延后，非正式成果遵守优先终止规则 | PASS by Task 5 tests and E2E source; live E2E remains skipped |
| 演练冲突策略支持暂停和终止 | PASS by Task 5 unit tests |
| 冻结或取消的测试/演练不能继续提交成果或产生截止提醒 | PASS by Task 5 tests |
| 测试成果保留 400 天，演练成果保留两年 | PASS by Task 6 retention tests; live E2E remains skipped |
| 测试和演练标识不可移除 | PASS by naming/marker unit tests; live E2E remains skipped |
| 运维 API、页面、worker、健康检查和审计记录完整 | PASS by focused tests and 1746 backend tests |
| 迁移、后端、前端、E2E、Ruff、类型检查、构建和 Compose 校验有真实证据 | SATISFIED WITH LIMITATION: backend 0 failed, frontend 216 passed, migration 32 passed, operations E2E explicitly skipped |
