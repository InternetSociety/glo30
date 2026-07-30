from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


@pytest.mark.asyncio
async def test_password_login_returns_jwt(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(password="correct-horse")

    response = await client.post(
        "/token",
        data={"username": "user@example.com", "password": "correct-horse"},
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


@pytest.mark.asyncio
async def test_regular_user_can_view_own_api_token(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()

    response = await client.get(
        "/manage-users",
        headers={"Authorization": "Bearer test-bearer-token"},
    )

    assert response.status_code == 200
    assert "test-bearer-token" in response.text


@pytest.mark.asyncio
async def test_admin_can_create_user_from_management_ui(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(
        email="admin@example.com",
        bearer_token=None,
        is_admin=True,
    )
    login = await client.post(
        "/login",
        data={"username": "admin@example.com", "password": "correct-horse"},
    )
    cookie = login.cookies["access_token"]

    response = await client.post(
        "/users/create",
        data={"email": "new@example.com", "password": "long-enough-password"},
        cookies={"access_token": cookie},
    )

    assert response.status_code == 303
    result = await db_session.execute(select(User).where(User.email == "new@example.com"))
    user = result.scalar_one()
    assert user.bearer_token is not None
    assert not user.is_admin
