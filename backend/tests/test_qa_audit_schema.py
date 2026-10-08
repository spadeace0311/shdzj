from __future__ import annotations

import subprocess
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

BACKEND_DIR = Path(__file__).parents[1]
AUDIT_REVISION = "0025_qa_audit_provenance"
PREVIOUS_REVISION = "0024_ai_knowledge_qa"


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["alembic", *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _current_revision() -> str:
    result = _alembic("current")
    revision_lines = [
        line.strip().split()[0]
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("INFO")
    ]
    assert revision_lines
    return revision_lines[-1]


def _set_revision(target: str) -> None:
    current = _current_revision()
    if current == target:
        return
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    target_revision = scripts.get_revision(target)
    current_ancestors: set[str] = set()
    pending = [scripts.get_revision(current)]
    while pending:
        revision = pending.pop()
        if revision.revision in current_ancestors:
            continue
        current_ancestors.add(revision.revision)
        down_revisions = revision.down_revision
        if down_revisions is None:
            continue
        if isinstance(down_revisions, tuple):
            pending.extend(scripts.get_revision(item) for item in down_revisions)
        else:
            pending.append(scripts.get_revision(down_revisions))
    if target_revision.revision in current_ancestors:
        _alembic("downgrade", target)
    else:
        _alembic("upgrade", target)


async def _qa_answer_columns() -> set[str]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("qa_answers")
                }
            )
    finally:
        await engine.dispose()


def test_qa_audit_migration_is_alembic_head() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))

    assert ScriptDirectory.from_config(config).get_current_head() == AUDIT_REVISION


async def test_qa_audit_provenance_columns_are_reversible() -> None:
    _set_revision(PREVIOUS_REVISION)
    try:
        assert await _qa_answer_columns() == {
            "id",
            "question_id",
            "status",
            "structured",
            "citation_keys",
            "degraded_reasons",
            "duration_ms",
            "created_at",
            "updated_at",
            "completed_at",
            "text",
        }

        _set_revision(AUDIT_REVISION)
        assert await _qa_answer_columns() == {
            "id",
            "question_id",
            "status",
            "model_name",
            "model_version",
            "prompt_version",
            "execution_plan",
            "tool_call_summary",
            "structured",
            "citation_keys",
            "degraded_reasons",
            "duration_ms",
            "created_at",
            "updated_at",
            "completed_at",
            "text",
        }

        _set_revision(PREVIOUS_REVISION)
        assert await _qa_answer_columns() == {
            "id",
            "question_id",
            "status",
            "structured",
            "citation_keys",
            "degraded_reasons",
            "duration_ms",
            "created_at",
            "updated_at",
            "completed_at",
            "text",
        }

        _set_revision(AUDIT_REVISION)
        assert "model_name" in await _qa_answer_columns()
    finally:
        _set_revision(AUDIT_REVISION)
