from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_data_asset_catalog(session: AsyncSession) -> None:
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended('data-asset-catalog', 0)"
            ")"
        )
    )
