# SDD ledger — plan: docs/superpowers/plans/2026-10-10-operations-test-drill-automation.md

## Plan Identity

- Branch: `codex/operations-automation`
- Starting commit: `5a5dcaf`
- Spec: `docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

## Preflight Scan

| Surface | Producer | Consumer | Finding |
| --- | --- | --- | --- |
| Task 1 models/config/migration | operations tables, enums, settings fields, `0026` | Tasks 2-9 | Consistent; `0025_qa_audit_provenance` is the current head and `0026_operations_automation` is the sole new head. |
| Task 1 -> Task 2 | models and settings | scenario/repository | Fixed undefined `settings` and `generator` in the Task 2 test snippet; `region_contains()` is now declared in both Interfaces and tests. |
| Task 2 -> Task 3 | `ScenarioGenerator`, `OperationsRepository` | service/API | Method names and session-based signatures are consistent. |
| Task 3 -> Task 4 | trigger/read service methods | scheduler | Trigger return type is used without introducing a second event model; scheduler owns plan state updates. |
| Task 3 -> Task 7 | operation endpoints and trigger results | frontend client/page | Endpoint set, role split, and frontend trigger modes agree. |
| Task 4 -> Task 6 | `DailyTestScheduler` and cycle counters | operations worker | Schedule states and result counters are represented in both tasks. |
| Task 5 -> Tasks 3/4 | priority guard and blocked work | manual trigger, scheduler, collaboration | Fixed the hook description to cover both `ingest()` and `ingest_collected()` because manual and formal reports use different paths. |
| Task 6 -> Task 9 | worker health and retention | Compose and final verification | Added the missing `process_health.py` modification and `operations` process mode. |
| Task 7 -> Task 8 | operations page/API behavior | E2E and runbook | Page actions map to the Task 3 API and preserve the dedicated manual formal-event route. |
| Task 1 -> Task 9 | migration upgrade/downgrade | final verification | `0026` is the only new revision and Task 9 keeps migration round-trip verification. |
| Task 1 self-check | tests vs models | Task 1 | Test constants and table set match the four planned tables. |
| Task 2 self-check | tests vs scenario code | Task 2 | Deterministic seed, Shanghai-boundary checks, and settings validation are represented. |
| Task 3 self-check | API tests vs router/errors | Task 3 | Permissions, idempotency, and error mappings agree. |
| Task 4 self-check | scheduler tests vs plan lifecycle | Task 4 | Window, idempotency, deferral, skip, and failure cases agree. |
| Task 5 self-check | priority tests vs guard integration | Task 5 | Test/cancel/pause/freeze/notification cases agree with guard behavior. |
| Task 6 self-check | worker tests vs cycle order | Task 6 | Worker order, retention, stop handling, and health freshness agree. |
| Task 7 self-check | page tests vs UI contract | Task 7 | Loading, empty, error, trigger, and superadmin states agree. |
| Task 8 self-check | E2E/docs vs implemented endpoints | Task 8 | Real-backend E2E and operational runbook consume only planned interfaces. |
| Task 9 self-check | acceptance checklist vs tasks | Task 9 | Every Global Constraint maps to at least one task or final verification step. |

Preflight rulings:

- Ruling: Task 2's test must create its own active boundary and settings record — the former fixture did not exist in the repository, and a missing fixture would force the implementer to invent test setup. Cost if wrong: minor test fixture rework.
- Ruling: real-event protection hooks both event service entry paths — manual and formal reports use different methods, so one hook would leave a priority bypass. Cost if wrong: manual formal events would not pause tests/drills.
- Ruling: operation worker health extends the existing `process_health` mode map — Compose already uses that helper for the other workers. Cost if wrong: healthcheck startup/configuration would need adjustment.

## Task Log

- Task 1: implementer `DONE_WITH_CONCERNS`, commit `e4058c9`, tests 21 passed; independent review found 2 Important and 4 Minor findings.
- Task 1: Ruling: `operations_test_schedules.event_id` uses `ON DELETE CASCADE` instead of the planned `SET NULL` — event retention should remove the associated plan, and `SET NULL` violates the triggered-state check constraint. Cost if wrong: retaining schedule history after event deletion would require an explicit archival table or pre-delete state rewrite.
- Task 1: fix round 1/5 dispatched to original implementer (2 Important, 4 Minor; commits pending).
- Task 1: fix round 1/5 commit `cc2b910`; scoped re-review says five findings addressed, one plan-deviation finding resolved by controller ruling, new Important gap found: migration cascade test omitted `OperationsRun`.
- Task 1: plan ruling formalized in commit `38c31ff`: retention deletion of test/drill events cascades to operations schedules and runs. Cost if wrong: schedule/run history is intentionally removed with the event after its retention cutoff.
- Task 1: fix round 2/5 dispatched to original implementer (add operations-run cascade coverage; remove unrelated formatting churn).
- Task 1: deferred Minor (out-of-scope observation): `OPERATIONS_TABLES` is duplicated across schema and migration tests; final review can decide whether to consolidate.
- Task 1: fix round 2/5 commit `4aea81a`; scoped re-review clean, all findings addressed, no new Critical/Important breakage.
- Task 1: complete (commits `5a5dcaf..4aea81a`, review clean; 1 deferred Minor).
- Task 2: implementer `DONE_WITH_CONCERNS`, commit `15ec80c`, focused tests 10 passed; independent review found 2 Important and 4 Minor findings.
- Task 2: Ruling: test scenario source is `operations`, while the brief-defined `source_event_id` remains `operations-test:{YYYYMMDD}:{seed}` — this avoids duplicate prefixes in canonical identifiers. Cost if wrong: ingestion history will use `operations` as the source label rather than `operations-test`.
- Task 2: Ruling: settings numeric patches must be validated and quantized before persistence so API errors and audit snapshots stay deterministic. Cost if wrong: a rejected or rounded patch could produce a misleading audit value.
- Task 2: plan amendments committed `813e8ec` and `5518834`; fix round 1/5 dispatched to original implementer (2 Important, 4 Minor).
- Task 2: fix round 1/5 commit `fe0fbb3`; scoped re-review approved all six findings, no new Critical/Important breakage.
- Task 2: complete (commits `4aea81a..fe0fbb3`, review clean; 2 deferred test-strength Minors).
- Task 2: deferred Minor (test strength): settings initialization concurrency test lacks a deterministic barrier.
- Task 2: deferred Minor (test portability): endpoint tests hard-code CPython `random.Random` sequences.
- Execution order Ruling: Task 5 runs before Task 3 — Task 3's service consumes `OperationsPriorityGuard`, so the guard must exist first; logical task numbers remain unchanged. Cost if wrong: task reports use non-sequential order, but interfaces are cleaner and no temporary guard is needed.
- Task 5 Ruling: include manual-event full assessment in Task 5 — the spec requires CENC-unpublished Shanghai-network formal events to start the same complete assessment, and `ingest()` is the existing manual entry point touched by this task, so leaving it as record-only would preserve a known spec violation. Cost if wrong: Task 5 has a larger diff and must cover manual outbox creation there rather than in a later task.
- Task 5 Ruling: define the initial active-real-event protection window as seven rolling days because the schema has no explicit real-event close field — this keeps daily test/drill work suppressed through the initial response period while allowing normal automation to recover automatically. Cost if wrong: the window must be tuned when operational close/end semantics are added.
- Task 5 Ruling: add `collaboration/router.py` to the task and map `OperationsWorkBlocked` to HTTP 409 `operations_work_blocked` — service-layer blocking alone would still leak an unhandled exception through the mutation API. Cost if wrong: one additional API file is touched in this task.
- Task 5: implementer `Parfit` (`01a126dc-d06e-71d3-ae01-a7d716f9f66f`) dispatched from base `979f189`.
- Task 5: implementer `DONE`, commit `ea50105`; focused regression 45 passed, event-service regression 59 passed; independent reviewer `Anscombe` (`01a126eb-4f79-7b41-9244-9a54c47b9cf1`) dispatched.
- Task 5: independent review found 2 Important (freeze check not serialized with real-event activation; deferred-release check/update race) and 3 Minor. Fix round 1/5 dispatched to original implementer.
- Task 5: fix round 1/5 commit `71734e6`; 2 concurrency tests added, focused regression 47 passed; scoped re-reviewer `Euler` (`01a126f6-f5dd-7542-913f-ae0baff42a6a`) dispatched.
- Task 5: fix round 1/5 (2 addressed, 0 open; commits `ea50105..71734e6`).
- Task 5: complete (commits `979f189..71734e6`, review clean after 1 fix round; 3 deferred Minors).
- Task 5 deferred Minor: validate `OperationsSettings.timezone` when settings are changed so `ZoneInfo` cannot fail during real-event ingestion.
- Task 5 deferred Minor: replace `_supports_query_session()` duck-typing no-op with an explicit session protocol/type boundary.
- Task 5 deferred Minor: strengthen tests for pre-existing notification preservation and direct upload/override/restore 409 mappings.
- Task 3: implementer dispatch base is `71734e6`.
- Task 3: implementer `Kant` (`01a126f8-8659-7110-9547-0b27acbeb338`) dispatched from base `71734e6`.
- Task 3: implementer `DONE`, commit `b2ac8a8`; focused tests 17 passed, neighboring regression 109 passed; independent review next.
- Task 3: independent review found 3 Important (cross-mode idempotency orphan event; real-event check/create race; over-broad non-CENC merge change) and 3 Minor. Fix round 1/5 dispatched to original implementer.
- Task 3: fix round 1/5 commit `e0ce7cc`; focused tests 20 passed, neighboring regression 112 passed; scoped re-reviewer `Lovelace` (`01a12714-4269-7303-ad1e-e8db5cbebab5`) dispatched.
- Task 3: fix round 1/5 (2 addressed, 1 open, 1 new Important scheduled-key confusion). Fix round 2/5 dispatched.
- Task 3: fix round 2/5 commit `14251b7`; focused tests 22 passed, neighboring regression 114 passed; scoped re-reviewer `Chandrasekhar the 2nd` (`01a1271a-5b8f-7ad0-abcc-8ce4b70286f4`) dispatched.
- Task 3: fix round 2/5 (2 addressed, 0 open; commits `e0ce7cc..14251b7`).
- Task 3: complete (commits `71734e6..14251b7`, review clean after 2 fix rounds; 3 deferred Minors).
- Task 3 deferred Minor: trigger routes use a broad `except Exception` and map unknown faults to storage unavailable.
- Task 3 deferred Minor: idempotent trigger replay appends another `triggered` audit row.
- Task 3 deferred Minor: positive permission tests do not cover superadmin and group_deputy triggers directly.
- Task 4: implementer dispatch base is `14251b7`.
- Task 4: implementer `Halley the 2nd` (`01a1271c-3c7f-7fa3-bcdf-7f1369edb6b7`) dispatched.
- Task 4: implementer `DONE`, commit `c366244`; focused scheduler tests 7 passed, neighboring operations regression 53 passed, ruff clean; independent reviewer `Gauss the 2nd` (`01a12728-8b12-7942-9920-9cd62316c595`) dispatched.
- Task 4: independent review found 1 Important (scheduler failures have no alert/audit action) and 2 Minor. Fix round 1/5 dispatched to original implementer.
- Task 4: deferred Minor (metrics semantics): disabled schedules are counted in `OperationsSchedulerResult.due`.
- Task 4: deferred Minor (test strength): the different-date randomness test monkeypatches a constant seed and does not verify variation.
- Task 4: fix round 1/5 commit `cf38e83`; focused tests 8 passed, neighboring operations regression 54 passed, ruff clean; scoped re-review says the Important finding is addressed and no new Critical/Important breakage.
- Task 4: fix round 1/5 (1 addressed, 0 open; commits `c366244..cf38e83`).
- Task 4: complete (commits `14251b7..cf38e83`, review clean after 1 fix round; 2 deferred Minors).
- Task 6: implementer dispatch base is `cf38e83`.
- Task 6: implementer `Carson the 2nd` (`01a1272e-5d96-70d2-a0ad-d846ab460439`) dispatched.
- Task 6: implementer `DONE`, commit `3269afa`; focused tests 22 passed, ruff clean, Compose config quiet, `git diff --check` clean; independent reviewer `Russell the 2nd` (`01a12733-750f-7290-bdcc-435ae200eca9`) dispatched.
- Task 6: independent review found 1 Important (`schedules_created` is always zero in the real worker path) and 4 Minor. Fix round 1/5 dispatched to original implementer.
- Task 6: Ruling: the plan's “刷新运行状态” is satisfied for this task by `OperationsPriorityGuard.release_deferred_runs()` plus the health heartbeat; no separate run-reconciliation interface is defined anywhere in the plan. Cost if wrong: asynchronous run-state reconciliation will need to be added before final acceptance.
- Task 6: Ruling: the `operations-worker` Compose healthcheck follows the plan verbatim via `app.process_health operations`; the separate FastAPI `/health` model is covered by unit tests and does not need a network server in this task. Cost if wrong: deployment health will report process liveness rather than heartbeat staleness until a later operational hardening task.
- Task 6: deferred Minor (logging): failed counts are logged at INFO instead of warning/error.
- Task 6: deferred Minor (deployment health): the Compose check does not consume the heartbeat-backed FastAPI health state.
- Task 6: deferred Minor (Compose polish): the operations worker healthcheck omits interval/timeout/retries/start_period used by neighboring services.
- Task 6: deferred Minor (upstream warning): `fastapi.testclient` emits a Starlette deprecation warning unrelated to this change.
- Task 6: fix round 1/5 commit `fa8b170`; focused worker/scheduler/health/retention regression 31 passed, ruff clean, Compose config quiet; scoped re-review says the Important finding is addressed and no new Critical/Important breakage.
- Task 6: fix round 1/5 (1 addressed, 0 open; commits `3269afa..fa8b170`).
- Task 6: complete (commits `cf38e83..fa8b170`, review clean after 1 fix round; 4 deferred Minors, 2 rulings).
- Task 7: implementer dispatch base is `fa8b170`.
- Task 7: implementer `Meitner the 2nd` (`01a12739-b171-7cd3-9a40-38c917b447b7`) dispatched.
- Task 7: implementer `DONE`, commit `c51f0e7`; focused page tests 10 passed, typecheck/build passed, full frontend suite 214 passed, Playwright 1920x1080 overflow/state checks passed; independent reviewer `Copernicus the 2nd` (`01a12746-a6ac-7b71-97b9-dbd34f753353`) dispatched.
- Task 7: independent review found 1 Important (deferred/canceled drill trigger responses are rendered as triggered) and 1 Minor. Fix round 1/5 dispatched to original implementer.
- Task 7: deferred Minor (permission affordance): trigger buttons remain visible to roles that the API rejects with 403.
- Task 7: fix round 1/5 commit `6411955`; focused page tests 12 passed, typecheck/build passed, full frontend suite 216 passed; scoped re-review says the Important finding is addressed and no new Critical/Important breakage.
- Task 7: fix round 1/5 (1 addressed, 0 open; commits `c51f0e7..6411955`).
- Task 7: complete (commits `fa8b170..6411955`, review clean after 1 fix round; 1 deferred Minor).
- Task 8: implementer dispatch base is `6411955`.
- Task 8: implementer `Feynman the 2nd` (`01a1274d-eaf1-7833-9fd9-f3248f2dbb8d`) dispatched.
- Task 8: implementer `DONE_WITH_CONCERNS`, commit `15a991f`; docs/static checks passed, E2E loads and skips because `E2E_SUPERADMIN_PASSWORD` is unavailable; independent reviewer `Hooke the 2nd` (`01a12754-f98e-72c0-8d7a-8fc1eb22b886`) dispatched.
- Task 8: independent review found 3 Important (uncleaned long-lived formal E2E event, incorrect worker health documentation, incomplete per-artifact test-marker assertions) and 3 Minor. Fix round 1/5 dispatched to original implementer.
- Task 8: deferred Minor (E2E health URL): the health check can target the frontend origin when `E2E_API_BASE_URL` is unset.
- Task 8: deferred Minor (runbook precision): active real-event conditions omit `current_revision_id` and the 7-day lower bound.
- Task 8: deferred Minor (runbook wording): `pause` is described as termination although Temporal is not canceled.
- Task 8: fix round 1/5 commit `909d110`; E2E remains explicitly skipped without credentials/purge gate, docs/static checks passed; scoped re-review says all 3 Important findings are addressed and no new Critical/Important breakage.
- Task 8: fix round 1/5 (3 addressed, 0 open; commits `15a991f..909d110`).
- Task 8: complete (commits `6411955..909d110`, review clean after 1 fix round; 3 deferred Minors; live E2E pending credentials).
- Task 9: implementer dispatch base is `909d110`.
- Task 9: implementer `Plato the 2nd` (`01a1275e-623d-7a91-bf28-7725aea0465d`) dispatched.
- Task 9: implementer `BLOCKED`, commit `e03dab3`; backend full suite 1742 passed, 3 skipped, 8 deselected, 4 failed; migration round-trip 32 passed; frontend default timezone 215 passed/1 failed and Shanghai timezone 216 passed; E2E explicitly skipped. Real blockers: stale QA-audit head assertion, timezone-dependent frontend test, and full backend command not starting Temporal.
- Task 9: fix round 1/5 dispatched to original implementer (fix QA migration-head ownership, deterministic Vitest timezone, document/start Temporal for full verification, rerun all suites).
- Task 9: backend full-suite command first collided with host port `5432`; rerun with project port `15433` produced `4 failed, 1742 passed, 3 skipped, 8 deselected`.
- Task 9: backend skips confirmed as `tests/test_data_asset_mdb_host_integration.py` (`3 skipped`, reason: Shanghai MDB is not installed on this host).
- Task 9: backend failures classified as three Temporal integration failures (`temporal` is not a dependency of `api` in the brief command) and one stale QA-audit migration assertion (`0025` vs current `0026`).
- Task 9: started `codex-operations` `temporal-postgres`/`temporal`, restored database to `0026_operations_automation`, and reran the three Temporal tests: `3 passed`; this confirms those three are environmental, not application regressions.
- Task 9: Step 3 migration round-trip command passed `32 passed`; `0026_operations_automation` upgrades, downgrades, re-upgrades, preserves default settings, and cascades schedule/run cleanup.
- Task 9: frontend `npm test` under the default container timezone failed `1 failed, 215 passed`; the failure is `workgroup-tasks.test.tsx` expecting Shanghai-local `10:00` as UTC `02:00`.
- Task 9: frontend `npm test` rerun with `TZ=Asia/Shanghai` passed `216 passed`; confirms the failure is timezone-portability, not an application regression.
- Task 9: frontend `operations.spec.ts` E2E returned `1 skipped` because `E2E_SUPERADMIN_PASSWORD` and `E2E_ALLOW_EVENT_PURGE=1` are absent; not recorded as pass.
- Task 9: final `alembic current` is `0026_operations_automation (head)`; `git diff --check` clean before commit.
- Task 9: independent verification outcome `BLOCKED`: full backend/frontend suites are not fully green without fixing the stale QA-audit test, the timezone-portable frontend test, and documenting/starting Temporal as a full-suite dependency.
- Task 9: no application code was changed by Task 9; existing deferred Minors and rulings remain open and are carried forward in `final-report.md`.
- Task 9: fix round 1/5 completed. QA-audit test now verifies `0025` as an ancestor of `0026` and restores `0026`; Vitest config pins `TZ=Asia/Shanghai`; README documents starting `postgres temporal-postgres temporal` before full backend verification.
- Task 9: post-fix backend full suite passed `1746 passed, 3 skipped, 8 deselected, 0 failed`; QA-audit focused regression `2 passed`; migration/operations worker `32 passed`; frontend `216 passed` without `TZ` override; operations E2E `1 skipped`; final `alembic current` `0026_operations_automation (head)`.
- Task 9: final status `DONE_WITH_CONCERNS`: live operations E2E remains pending credentials/purge authorization; Shanghai MDB host integration remains skipped; existing deferred Minors remain open.

## Progress

- [x] Task 1: 运维 schema、配置与 `0026` 迁移
- [x] Task 2: 上海测试场景生成与计划设置
- [x] Task 3: 运维服务和手工触发 API
- [x] Task 4: 每日随机测试调度
- [x] Task 5: 真实事件优先与任务冻结
- [x] Task 6: 运维工作进程与保留调度
- [x] Task 7: 运维前端与手工入口
- [x] Task 8: E2E、运行手册和交接说明
- [x] Task 9: 子系统全量验证与最终报告（DONE_WITH_CONCERNS）
