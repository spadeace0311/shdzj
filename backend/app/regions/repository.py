from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.regions.models import RegionBoundary


class RegionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    ) -> None:
        self._session_factory = session_factory

    async def get_active(self, session: AsyncSession) -> RegionBoundary | None:
        return await session.scalar(
            select(RegionBoundary).where(RegionBoundary.is_active.is_(True))
        )

    async def activate(self, version: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                target_id = await session.scalar(
                    select(RegionBoundary.id).where(RegionBoundary.version == version)
                )
                if target_id is None:
                    raise ValueError(f"unknown region boundary version: {version}")

                await session.execute(
                    update(RegionBoundary)
                    .where(RegionBoundary.is_active.is_(True))
                    .values(is_active=False)
                )
                await session.execute(
                    update(RegionBoundary)
                    .where(RegionBoundary.id == target_id)
                    .values(is_active=True)
                )
