from sqlalchemy import inspect

from app.db import engine


KNOWLEDGE_TABLES = {
    "knowledge_sources",
    "knowledge_source_versions",
    "knowledge_chunks",
    "knowledge_index_versions",
    "knowledge_jobs",
    "knowledge_web_snapshots",
    "knowledge_snapshots",
}
QA_TABLES = {
    "qa_sessions",
    "qa_questions",
    "qa_answers",
    "qa_citations",
    "qa_tool_calls",
    "qa_map_actions",
    "qa_feedback",
    "qa_admin_audit_logs",
}


async def test_knowledge_and_qa_tables_exist() -> None:
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    assert KNOWLEDGE_TABLES <= names
    assert QA_TABLES <= names
