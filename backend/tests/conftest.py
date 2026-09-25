import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://earthquake:earthquake@postgres:5432/earthquake",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-16-characters")
os.environ.setdefault(
    "SUPERADMIN_INITIAL_PASSWORD",
    "test-superadmin-password-at-least-16-characters",
)
