from collections.abc import AsyncIterator, Awaitable, Callable

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services.auth import hash_password


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()
    await test_engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.flush()

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def create_test_user(
    db_session: AsyncSession,
) -> Callable[..., Awaitable[User]]:
    async def create(
        *,
        email: str = "user@example.com",
        password: str = "correct-horse",
        bearer_token: str | None = "test-bearer-token",
        is_admin: bool = False,
        is_active: bool = True,
    ) -> User:
        user = User(
            email=email,
            password_hash=hash_password(password),
            bearer_token=bearer_token,
            is_admin=is_admin,
            is_active=is_active,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return create
