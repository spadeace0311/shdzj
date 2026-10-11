# SDD ledger - plan: docs/superpowers/plans/2026-10-11-operations-safety-residuals.md

## Identity

- Branch: `codex/operations-automation`
- Starting commit: `670e037`
- Source review: `.superpowers/sdd/2026-10-10-operations-test-drill-automation/re-review-89cec18.md`
- Spec: `docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

## Preflight Scan

| Surface | Producer | Consumer | Finding |
| --- | --- | --- | --- |
| Task 1 compatibility lookup | existing legacy/current scheduled run | scheduler dispatch | Need both keys recognized before ingest. |
| Task 1 conflict recovery | `_finalize_trigger` | existing event | New duplicate event must be deleted only after finding the existing schedule-owned run. |
| Task 2 reconciliation | assessment/production statuses | operations state | Missing production rows must not become completed. |
| Task 2 resume | source paused run | replacement run | Deterministic key and source terminalization are required. |
| Task 3 scenario context | locked boundary | event ingestion | Context must be created before the generation transaction commits. |
| Task 4 retention | production dependencies | artifact deletion | Active runs/tasks and the disabled flag must protect rows. |
| Task 5 timezone repair | update patch | persisted settings | Load without validation, then validate the repaired result. |
| Task 6 E2E health fallback | environment helper | Playwright health request | Empty strings must fall back to API base. |
| Task 7 verification | Tasks 1-6 | final review | Must use codex-operations and preserve E2E skip honesty. |

Preflight rulings:

- Ruling: implement the residual work as a new plan rather than a second fix wave inside the previous SDD review. Cost if wrong: an additional review cycle and plan artifact are required.
- Ruling: do not change existing migration 0026/0027 unless a test proves schema storage is required. Cost if wrong: a later migration may be needed if detail JSON linkage is insufficient.
- Ruling: keep live E2E skipped unless both real credentials and purge authorization are available. Cost if wrong: one external workflow remains unverified.

## Task Log

- Task 1: pending.
- Task 2: pending.
- Task 3: pending.
- Task 4: pending.
- Task 5: pending.
- Task 6: pending.
- Task 7: pending.

## Progress

- [ ] Task 1: scheduler upgrade recovery
- [ ] Task 2: run reconciliation and idempotent drill resume
- [ ] Task 3: atomic scenario boundary handoff
- [ ] Task 4: active artifact retention protection
- [ ] Task 5: invalid timezone repair
- [ ] Task 6: E2E health URL fallback
- [ ] Task 7: residual whole-branch verification
