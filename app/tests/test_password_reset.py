import asyncio
import hashlib
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.dependencies import get_user_service
from app.main import app
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.auth import CSRF_COOKIE_NAME, verify_password
from app.services.email import send_password_reset
from app.services.users import UserService

ResetSender = Callable[[str, str, Settings], None]


def enable_password_reset(
    db_session: AsyncSession,
    sender: ResetSender,
) -> None:
    app.dependency_overrides[get_user_service] = lambda: UserService(
        UserRepository(db_session),
        Settings(
            _env_file=None,
            smtp_enabled=True,
            smtp_host="mail.example.com",
            smtp_from="no-reply@example.com",
        ),
        sender,
    )


async def request_reset(client: AsyncClient, email: str) -> tuple[int, str, str]:
    if CSRF_COOKIE_NAME not in client.cookies:
        await client.get("/forgot-password")
    response = await client.post(
        "/forgot-password",
        data={"email": email, "csrf_token": client.cookies[CSRF_COOKIE_NAME]},
    )
    return response.status_code, response.headers["location"], response.text


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_reset_email_contains_the_code_in_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_messages: list[EmailMessage] = []
    connection: list[tuple[str, int, int]] = []

    class FakeSmtp:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            connection.append((host, port, timeout))

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def send_message(self, message: EmailMessage) -> None:
            sent_messages.append(message)

    monkeypatch.setattr("app.services.email.smtplib.SMTP", FakeSmtp)
    reset_code = "plain-text-reset-code"
    app_settings = Settings(
        _env_file=None,
        smtp_enabled=True,
        smtp_host="mail.example.com",
        smtp_port=2525,
        smtp_from="no-reply@example.com",
    )

    send_password_reset("user@example.com", reset_code, app_settings)

    message = sent_messages[0]
    assert connection == [("mail.example.com", 2525, 10)]
    assert message["To"] == "user@example.com"
    assert message["From"] == "no-reply@example.com"
    content = message.get_content()
    assert reset_code in content
    assert "/reset-password" in content
    assert "30 minutes" in content


@pytest.mark.asyncio
async def test_reset_request_is_generic_and_stores_only_a_30_minute_digest(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    active = await create_test_user()
    await create_test_user(
        email="inactive@example.com",
        bearer_token="inactive-token",
        is_active=False,
    )
    deliveries: list[tuple[str, str]] = []

    def sender(recipient: str, reset_code: str, _settings: Settings) -> None:
        deliveries.append((recipient, reset_code))

    enable_password_reset(db_session, sender)

    unknown = await request_reset(client, "unknown@example.com")
    inactive = await request_reset(client, "inactive@example.com")
    active_response = await request_reset(client, active.email)

    assert unknown == inactive == active_response
    assert active_response[:2] == (303, "/forgot-password?sent=1")
    assert deliveries[0][0] == active.email
    reset_code = deliveries[0][1]
    assert active.reset_token_hash == hashlib.sha256(reset_code.encode()).hexdigest()
    assert reset_code not in active.reset_token_hash
    assert active.reset_token_expires_at is not None
    lifetime = as_utc(active.reset_token_expires_at) - datetime.now(UTC)
    assert timedelta(minutes=29, seconds=50) < lifetime <= timedelta(minutes=30)


@pytest.mark.asyncio
async def test_valid_reset_is_single_use_and_changes_the_password(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    codes: list[str] = []

    def sender(_recipient: str, reset_code: str, _settings: Settings) -> None:
        codes.append(reset_code)

    enable_password_reset(db_session, sender)
    await request_reset(client, user.email)

    reset = await client.post(
        "/reset-password",
        data={
            "reset_code": codes[0],
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )
    reused = await client.post(
        "/reset-password",
        data={
            "reset_code": codes[0],
            "new_password": "another-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    assert reset.status_code == 303
    assert reset.headers["location"] == "/?reset=1"
    assert codes[0] not in reset.headers["location"]
    assert await asyncio.to_thread(verify_password, "replacement-password", user.password_hash)
    assert user.reset_token_hash is None
    assert user.reset_token_expires_at is None
    assert reused.status_code == 400
    assert "The reset code is invalid or expired." in reused.text


@pytest.mark.asyncio
async def test_invalid_expired_and_inactive_reset_codes_do_not_change_passwords(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    original_password_hash = user.password_hash
    enable_password_reset(db_session, lambda _recipient, _code, _settings: None)
    await client.get("/reset-password")

    invalid = await client.post(
        "/reset-password",
        data={
            "reset_code": "invalid-code",
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    expired_code = "expired-code"
    user.reset_token_hash = hashlib.sha256(expired_code.encode()).hexdigest()
    user.reset_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    expired = await client.post(
        "/reset-password",
        data={
            "reset_code": expired_code,
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    inactive_code = "inactive-code"
    user.is_active = False
    user.reset_token_hash = hashlib.sha256(inactive_code.encode()).hexdigest()
    user.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    inactive = await client.post(
        "/reset-password",
        data={
            "reset_code": inactive_code,
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    assert invalid.status_code == expired.status_code == inactive.status_code == 400
    assert user.password_hash == original_password_hash


@pytest.mark.asyncio
async def test_second_reset_request_invalidates_the_first_code(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    codes: list[str] = []
    enable_password_reset(
        db_session,
        lambda _recipient, code, _settings: codes.append(code),
    )

    await request_reset(client, user.email)
    await request_reset(client, user.email)
    first = await client.post(
        "/reset-password",
        data={
            "reset_code": codes[0],
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )
    second = await client.post(
        "/reset-password",
        data={
            "reset_code": codes[1],
            "new_password": "replacement-password",
            "csrf_token": client.cookies[CSRF_COOKIE_NAME],
        },
    )

    assert first.status_code == 400
    assert second.status_code == 303


@pytest.mark.asyncio
async def test_delivery_failure_clears_reset_state_without_changing_the_response(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await create_test_user()
    attempted_codes: list[str] = []

    def failing_sender(_recipient: str, code: str, _settings: Settings) -> None:
        attempted_codes.append(code)
        raise RuntimeError("SMTP unavailable")

    enable_password_reset(db_session, failing_sender)
    with caplog.at_level("WARNING", logger="uvicorn.error"):
        response = await request_reset(client, user.email)

    assert response[:2] == (303, "/forgot-password?sent=1")
    assert user.reset_token_hash is None
    assert user.reset_token_expires_at is None
    assert "Password reset email delivery failed" in caplog.text
    assert user.email not in caplog.text
    assert attempted_codes[0] not in caplog.text


@pytest.mark.asyncio
async def test_password_reset_delivery_does_not_block_the_event_loop(
    client: AsyncClient,
    db_session: AsyncSession,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    user = await create_test_user()
    started = threading.Event()
    release = threading.Event()

    def slow_sender(_recipient: str, _code: str, _settings: Settings) -> None:
        started.set()
        release.wait(timeout=1)

    enable_password_reset(db_session, slow_sender)
    await client.get("/forgot-password")
    request = asyncio.create_task(
        client.post(
            "/forgot-password",
            data={"email": user.email, "csrf_token": client.cookies[CSRF_COOKIE_NAME]},
        )
    )

    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    assert not request.done()
    release.set()
    response = await request

    assert response.status_code == 303


@pytest.mark.asyncio
async def test_password_reset_pages_use_public_navigation_and_csrf(client: AsyncClient) -> None:
    for path in ("/forgot-password", "/reset-password"):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'class="navbar navbar-expand-lg bg-dark navbar-dark"' in response.text
        assert response.text.index("<nav") < response.text.index("<main")
        assert 'name="csrf_token"' in response.text

    missing_csrf = await client.post(
        "/forgot-password",
        data={"email": "user@example.com"},
    )
    assert missing_csrf.status_code == 403
