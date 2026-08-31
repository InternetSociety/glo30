from app.models.cached_tile import CachedTile
from app.repositories.cached_tiles import CachedTileRepository


class TileCacheService:
    def __init__(self, repository: CachedTileRepository) -> None:
        self.repository = repository

    async def list_tiles(self) -> list[CachedTile]:
        return await self.repository.list_all()
