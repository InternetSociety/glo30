from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.models.user import User
from app.services.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME


async def sign_in(client: AsyncClient, email: str, password: str = "correct-horse") -> None:
    await client.get("/")
    response = await client.post(
        "/login",
        data={
            "username": email,
            "password": password,
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_anonymous_documentation_and_management_are_protected(client: AsyncClient) -> None:
    for path in ("/docs", "/app-docs", "/manage-users"):
        response = await client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    openapi = await client.get("/openapi.json")
    assert openapi.status_code == 401
    assert openapi.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_invalid_and_inactive_accounts_do_not_create_sessions(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(is_active=False)
    await client.get("/")
    csrf_token = client.cookies[CSRF_COOKIE_NAME]

    invalid = await client.post(
        "/login",
        data={
            "username": "missing@example.com",
            "password": "correct-horse",
            "csrf_token": csrf_token,
        },
    )
    inactive = await client.post(
        "/login",
        data={
            "username": "user@example.com",
            "password": "correct-horse",
            "csrf_token": csrf_token,
        },
    )

    assert invalid.headers["location"] == "/?error=invalid_credentials"
    assert inactive.headers["location"] == "/?error=inactive"
    assert SESSION_COOKIE_NAME not in client.cookies
    api = await client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer test-bearer-token"},
    )
    assert api.status_code == 403


@pytest.mark.asyncio
async def test_persistent_token_and_jwt_authenticate_api_requests(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()

    persistent = await client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer test-bearer-token"},
    )
    token = await client.post(
        "/token",
        data={"username": "user@example.com", "password": "correct-horse"},
    )
    jwt_request = await client.get(
        "/api/v1/health",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    )

    assert persistent.status_code == 200
    assert jwt_request.status_code == 200


@pytest.mark.asyncio
async def test_authenticated_pages_and_swagger_use_the_common_navigation(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(email="admin@example.com", bearer_token=None, is_admin=True)
    await sign_in(client, "admin@example.com")

    labels = [">API<", ">Guide<", ">Users<", ">Tile cache<", ">Sign out<"]
    for path in ("/", "/app-docs", "/manage-users", "/tile-cache", "/docs"):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'class="navbar navbar-expand-lg bg-dark navbar-dark"' in response.text
        if path != "/docs":
            assert response.text.index("<nav") < response.text.index("<main")
        navigation = response.text[response.text.index("<nav") : response.text.index("</nav>")]
        positions = [navigation.index(label) for label in labels]
        assert positions == sorted(positions)
        assert 'action="/logout" method="post"' in response.text


@pytest.mark.asyncio
async def test_swagger_preauthorizes_users_but_not_administrators(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()
    user_docs = await client.get(
        "/docs",
        headers={"Authorization": "Bearer test-bearer-token"},
    )
    assert "preauthorizeApiKey" in user_docs.text
    assert "test-bearer-token" in user_docs.text

    await create_test_user(
        email="admin@example.com",
        bearer_token="misconfigured-admin-token",
        is_admin=True,
    )
    admin_docs = await client.get(
        "/docs",
        headers={"Authorization": "Bearer misconfigured-admin-token"},
    )
    assert "preauthorizeApiKey" not in admin_docs.text
    assert "misconfigured-admin-token" not in admin_docs.text


@pytest.mark.asyncio
async def test_user_pages_never_render_password_hashes(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    response = await client.get(
        "/manage-users",
        headers={"Authorization": "Bearer test-bearer-token"},
    )

    assert response.status_code == 200
    assert user.password_hash not in response.text


@pytest.mark.asyncio
async def test_cookie_authenticated_mutations_require_csrf(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(email="admin@example.com", bearer_token=None, is_admin=True)
    await sign_in(client, "admin@example.com")

    missing = await client.post(
        "/users/create",
        data={"email": "new@example.com", "password": "long-enough-password"},
    )
    invalid = await client.post(
        "/logout",
        data={"csrf_token": "invalid"},
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_replacement_token_invalidates_the_old_token(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    await create_test_user(
        email="admin@example.com",
        bearer_token=None,
        is_admin=True,
    )
    await sign_in(client, "admin@example.com")

    response = await client.post(
        f"/users/{user.id}/regen-token",
        data={"csrf_token": client.cookies[CSRF_COOKIE_NAME]},
    )
    old_token = await client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer test-bearer-token"},
    )
    new_token = await client.get(
        "/api/v1/health",
        headers={"Authorization": f"Bearer {user.bearer_token}"},
    )

    assert response.status_code == 303
    assert old_token.status_code == 401
    assert new_token.status_code == 200


@pytest.mark.asyncio
async def test_non_administrator_cannot_call_administrator_routes(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()
    response = await client.post(
        "/users/create",
        headers={"Authorization": "Bearer test-bearer-token"},
        data={
            "email": "blocked@example.com",
            "password": "long-enough-password",
            "csrf_token": "irrelevant",
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_session_cookie_has_required_browser_security_attributes(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user(email="admin@example.com", bearer_token=None, is_admin=True)
    await client.get("/")
    response = await client.post(
        "/login",
        data={
            "username": "admin@example.com",
            "password": "correct-horse",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    session_cookie = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "Max-Age=" in session_cookie
