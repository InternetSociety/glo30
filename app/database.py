from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ensure_data_directories() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.tile_cache_dir.mkdir(parents=True, exist_ok=True)

    if settings.database_url.startswith("sqlite+aiosqlite:////"):
        database_path = Path(settings.database_url.removeprefix("sqlite+aiosqlite:////"))
        database_path.parent.mkdir(parents=True, exist_ok=True)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
