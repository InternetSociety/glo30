from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cached_tile import CachedTile
from app.models.user import User


async def add_cached_tile(
    db_session: AsyncSession,
    *,
    tile_id: str,
    last_used_at: datetime,
) -> CachedTile:
    tile = CachedTile(
        tile_id=tile_id,
        object_key=f"CCM/example/{tile_id}/DEM/{tile_id}_DEM.tif",
        file_path=f"/app/data/tiles/{tile_id}_DEM.tif",
        last_used_at=last_used_at,
        expires_at=last_used_at + timedelta(days=30),
    )
    db_session.add(tile)
    await db_session.flush()
    return tile


@pytest.mark.asyncio
async def test_tile_cache_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/tile-cache")

    assert response.status_code == 307
    assert response.headers["location"] == "/"


@pytest.mark.asyncio
async def test_regular_user_can_view_cached_tiles(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()
    now = datetime.now(UTC)
    older = await add_cached_tile(
        db_session,
        tile_id="Copernicus_DSM_10_S41_00_E174_00",
        last_used_at=now - timedelta(hours=1),
    )
    newer = await add_cached_tile(
        db_session,
        tile_id="Copernicus_DSM_10_S40_00_E174_00",
        last_used_at=now,
    )
    headers = {"Authorization": "Bearer test-bearer-token"}

    response = await client.get("/tile-cache", headers=headers)
    home = await client.get("/", headers=headers)

    assert response.status_code == 200
    assert "Tile Cache" in response.text
    assert "user@example.com" in response.text
    assert newer.tile_id in response.text
    assert newer.object_key in response.text
    assert newer.file_path in response.text
    assert response.text.index(newer.tile_id) < response.text.index(older.tile_id)
    assert 'href="/tile-cache"' in home.text


@pytest.mark.asyncio
async def test_admin_user_can_view_tile_cache(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(
        email="admin@example.com",
        password="correct-horse",
        bearer_token=None,
        is_admin=True,
    )
    await client.post(
        "/login",
        data={"username": "admin@example.com", "password": "correct-horse"},
    )

    response = await client.get("/tile-cache")

    assert response.status_code == 200
    assert "admin@example.com" in response.text
    assert "No tiles are currently cached." in response.text
