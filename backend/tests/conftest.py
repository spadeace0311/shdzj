import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://earthquake:earthquake@postgres:5432/earthquake",
)
