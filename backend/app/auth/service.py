from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import User
from app.config import settings
from app.security import hash_password, verify_password


class InvalidCredentialsError(Exception):
    """Raised when a username or password cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class AuthUser:
    username: str
    role: str
    workgroup: str | None
    is_active: bool = True


class UserRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def get_by_username(self, session: AsyncSession, username: str) -> User | None:
        return await session.scalar(select(User).where(User.username == username))

    async def create(
        self,
        session: AsyncSession,
        username: str,
        password_hash: str,
        role: str,
        workgroup: str | None = None,
        is_active: bool = True,
    ) -> User:
        user = User(
            username=username,
            password_hash=password_hash,
            role=role,
            workgroup=workgroup,
            is_active=is_active,
        )
        session.add(user)
        await session.flush()
        return user


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: UserRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or UserRepository(session_factory)

    async def authenticate(self, username: str, password: str) -> AuthUser:
        async with self._session_factory() as session:
            user = await self._repository.get_by_username(session, username)
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError
        return _to_auth_user(user)

    async def get_active_user(self, username: str) -> AuthUser:
        async with self._session_factory() as session:
            user = await self._repository.get_by_username(session, username)
        if user is None or not user.is_active:
            raise InvalidCredentialsError
        return _to_auth_user(user)

    async def bootstrap_superadmin(self) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await ensure_superadmin(session, self._repository)


async def ensure_superadmin(session: AsyncSession, repository: UserRepository) -> User:
    existing = await repository.get_by_username(session, settings.superadmin_username)
    if existing is not None:
        return existing
    return await repository.create(
        session,
        username=settings.superadmin_username,
        password_hash=hash_password(settings.superadmin_initial_password.get_secret_value()),
        role="superadmin",
        workgroup=None,
        is_active=True,
    )


def _to_auth_user(user: User) -> AuthUser:
    return AuthUser(
        username=user.username,
        role=user.role,
        workgroup=user.workgroup,
        is_active=user.is_active,
    )
