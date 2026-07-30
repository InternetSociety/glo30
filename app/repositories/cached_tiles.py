from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cached_tile import CachedTile


class CachedTileRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_tile_id(self, tile_id: str) -> CachedTile | None:
        result = await self.db.execute(select(CachedTile).where(CachedTile.tile_id == tile_id))
        return result.scalar_one_or_none()

    async def list_expired(self, now: datetime) -> list[CachedTile]:
        result = await self.db.execute(select(CachedTile).where(CachedTile.expires_at < now))
        return list(result.scalars().all())

    async def list_all(self) -> list[CachedTile]:
        result = await self.db.execute(select(CachedTile).order_by(CachedTile.last_used_at.desc()))
        return list(result.scalars().all())

    async def add(self, tile: CachedTile) -> CachedTile:
        self.db.add(tile)
        await self.db.flush()
        return tile

    async def delete(self, tile: CachedTile) -> None:
        await self.db.delete(tile)
        await self.db.flush()
