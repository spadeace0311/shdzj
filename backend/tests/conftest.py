import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://earthquake:earthquake@postgres:5432/earthquake",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-16-characters")
os.environ.setdefault(
    "SUPERADMIN_INITIAL_PASSWORD",
    "test-superadmin-password-at-least-16-characters",
)
os.environ.setdefault("CENC_COLLECTOR_ENABLED", "false")
