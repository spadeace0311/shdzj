from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from app.auth.router import get_auth_service, get_current_user
from app.auth.service import AuthUser, InvalidCredentialsError, ensure_superadmin
from app.config import settings
from app.events.domain import EventKind
from app.events.repository import EventIngestResult
from app.events.router import get_event_service
from app.main import app
from app.security import create_access_token, hash_password, verify_password

SUPERADMIN_USERNAME = "superadmin"
SUPERADMIN_PASSWORD = "test-superadmin-password-at-least-16-characters"
_MISSING = object()


@dataclass(slots=True)
class FakeUser:
    username: str
    password_hash: str
    role: str
    workgroup: str | None = None
    is_active: bool = True


class FakeAuthService:
    def __init__(self, users: list[FakeUser] | None = None) -> None:
        self._users = {user.username: user for user in (users or [])}

    async def authenticate(self, username: str, password: str) -> AuthUser:
        user = self._users.get(username)
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError
        return AuthUser(username=user.username, role=user.role, workgroup=user.workgroup)

    async def get_active_user(self, username: str) -> AuthUser:
        user = self._users.get(username)
        if user is None or not user.is_active:
            raise InvalidCredentialsError
        return AuthUser(username=user.username, role=user.role, workgroup=user.workgroup)


class FakeEventService:
    async def ingest(
        self,
        raw_payload: dict[str, object],
        event: object,
        received_at: datetime | None = None,
    ) -> EventIngestResult:
        del raw_payload, received_at
        kind = getattr(event, "kind", EventKind.MANUAL)
        return EventIngestResult(
            event_id="event-auth-1",
            revision_id="revision-auth-1",
            revision_no=1,
            event_kind=kind,
            is_current=True,
        )


@contextmanager
def _client(
    *,
    auth_service: FakeAuthService | None = None,
    current_user: AuthUser | None = None,
    event_service: FakeEventService | None = None,
) -> Iterator[TestClient]:
    overrides = {
        get_auth_service: auth_service,
        get_current_user: current_user,
        get_event_service: event_service,
    }
    previous: dict[object, object] = {}
    for dependency, value in overrides.items():
        previous[dependency] = app.dependency_overrides.get(dependency, _MISSING)
        if value is None:
            app.dependency_overrides.pop(dependency, None)
        else:
            app.dependency_overrides[dependency] = lambda value=value: value
    try:
        yield TestClient(app)
    finally:
        for dependency, value in previous.items():
            if value is _MISSING:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = value


def _manual_payload() -> dict[str, object]:
    return {
        "origin_time": "2026-09-17T02:30:05Z",
        "longitude": 121.54,
        "latitude": 31.22,
        "magnitude": 3.2,
        "depth_km": 8.0,
        "source": "shanghai-network",
    }


def test_superadmin_can_log_in() -> None:
    service = FakeAuthService(
        [FakeUser(SUPERADMIN_USERNAME, hash_password(SUPERADMIN_PASSWORD), "superadmin")]
    )

    with _client(auth_service=service) as client:
        response = client.post(
            "/api/v1/auth/login",
            data={"username": SUPERADMIN_USERNAME, "password": SUPERADMIN_PASSWORD},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_with_wrong_password_returns_401() -> None:
    service = FakeAuthService(
        [FakeUser(SUPERADMIN_USERNAME, hash_password(SUPERADMIN_PASSWORD), "superadmin")]
    )

    with _client(auth_service=service) as client:
        response = client.post(
            "/api/v1/auth/login",
            data={"username": SUPERADMIN_USERNAME, "password": "not-the-right-password"},
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_login_for_inactive_user_returns_401() -> None:
    service = FakeAuthService(
        [
            FakeUser(
                SUPERADMIN_USERNAME,
                hash_password(SUPERADMIN_PASSWORD),
                "superadmin",
                is_active=False,
            )
        ]
    )

    with _client(auth_service=service) as client:
        response = client.post(
            "/api/v1/auth/login",
            data={"username": SUPERADMIN_USERNAME, "password": SUPERADMIN_PASSWORD},
        )

    assert response.status_code == 401


def test_me_returns_current_user_without_password_hash() -> None:
    service = FakeAuthService(
        [FakeUser(SUPERADMIN_USERNAME, hash_password(SUPERADMIN_PASSWORD), "superadmin", "sh")]
    )

    with _client(auth_service=service) as client:
        login = client.post(
            "/api/v1/auth/login",
            data={"username": SUPERADMIN_USERNAME, "password": SUPERADMIN_PASSWORD},
        )
        token = login.json()["access_token"]
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {"username": SUPERADMIN_USERNAME, "role": "superadmin", "workgroup": "sh"}
    assert "password_hash" not in body


def test_me_without_token_returns_401() -> None:
    with _client() as client:
        response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_with_corrupted_token_returns_401() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer not-a-valid-token"},
        )

    assert response.status_code == 401


def test_me_with_expired_token_returns_401() -> None:
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": SUPERADMIN_USERNAME,
            "role": "superadmin",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with _client() as client:
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {expired}"},
        )

    assert response.status_code == 401


def test_manual_event_allows_supported_roles() -> None:
    for role in ("superadmin", "group_leader", "group_deputy"):
        current_user = AuthUser(username=f"{role}-user", role=role, workgroup=None)

        with _client(current_user=current_user, event_service=FakeEventService()) as client:
            response = client.post("/api/v1/events/manual", json=_manual_payload())

        assert response.status_code == 201


def test_manual_event_rejects_disallowed_role() -> None:
    current_user = AuthUser(username="viewer-user", role="viewer", workgroup=None)

    with _client(current_user=current_user, event_service=FakeEventService()) as client:
        response = client.post("/api/v1/events/manual", json=_manual_payload())

    assert response.status_code == 403


def test_manual_event_rejects_anonymous_user_with_401_not_422() -> None:
    with _client(event_service=FakeEventService()) as client:
        response = client.post("/api/v1/events/manual", json=_manual_payload())

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_superadmin_bootstrap_is_idempotent() -> None:
    repository = FakeUserRepository()
    session = object()

    await ensure_superadmin(session, repository)

    repository.created[0].password_hash = "already-existing-hash"
    repository.created[0].role = "viewer"
    await ensure_superadmin(session, repository)

    assert len(repository.created) == 1
    assert (
        repository.created[0].password_hash
        == repository.get_by_username_sync(SUPERADMIN_USERNAME).password_hash
    )
    assert repository.created[0].role == "viewer"


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    hashed = hash_password(SUPERADMIN_PASSWORD)

    assert hashed != SUPERADMIN_PASSWORD
    assert SUPERADMIN_PASSWORD not in hashed
    assert verify_password(SUPERADMIN_PASSWORD, hashed)


def test_access_token_contains_expected_claims() -> None:
    token = create_access_token("alice", "group_leader")
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
    )

    assert payload["sub"] == "alice"
    assert payload["role"] == "group_leader"
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["exp"], int)


class FakeUserRepository:
    def __init__(self) -> None:
        self.users: list[FakeUser] = []
        self.created: list[FakeUser] = []

    async def get_by_username(self, session: object, username: str) -> FakeUser | None:
        del session
        return next((user for user in self.users if user.username == username), None)

    async def create(
        self,
        session: object,
        username: str,
        password_hash: str,
        role: str,
        workgroup: str | None = None,
        is_active: bool = True,
    ) -> FakeUser:
        del session
        user = FakeUser(
            username=username,
            password_hash=password_hash,
            role=role,
            workgroup=workgroup,
            is_active=is_active,
        )
        self.users.append(user)
        self.created.append(user)
        return user

    def get_by_username_sync(self, username: str) -> FakeUser:
        return next(user for user in self.users if user.username == username)
