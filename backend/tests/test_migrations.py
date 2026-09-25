import ast
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations" / "versions"
ALEMBIC_VERSION_LENGTH = 32


def test_migration_identifiers_fit_alembic_version_column() -> None:
    oversized: list[str] = []

    for migration in MIGRATIONS_DIR.glob("*.py"):
        tree = ast.parse(migration.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            if node.target.id not in {"revision", "down_revision"}:
                continue

            value = ast.literal_eval(node.value)
            identifiers = value if isinstance(value, tuple) else (value,)
            for identifier in identifiers:
                if identifier is not None and len(identifier) > ALEMBIC_VERSION_LENGTH:
                    oversized.append(f"{migration.name}:{node.target.id}={identifier}")

    assert oversized == []
