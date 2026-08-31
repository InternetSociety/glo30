from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.repositories.cached_tiles import CachedTileRepository
from app.repositories.users import UserRepository
from app.services.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    csrf_token_is_valid,
    decode_access_token,
)
from app.services.s3_tiles import S3TileService
from app.services.tile_cache import TileCacheService
from app.services.users import UserService
from app.services.viewshed import ViewshedProcessor, ViewshedService

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="A persistent API token issued on the user-management page.",
)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    token = credentials.credentials if credentials else request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    repository = UserRepository(db)
    user = await repository.get_by_bearer_token(token)
    if user is not None:
        return user

    email = decode_access_token(token, settings)
    return await repository.get_by_email(email) if email else None


async def get_current_active_user(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user)],
) -> User:
    if current_user is None:
        if request.url.path.startswith("/api"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/"},
        )
    if not current_user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return current_user


async def get_current_admin_user(
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required",
        )
    return current_user


def get_user_service(db: Annotated[AsyncSession, Depends(get_db)]) -> UserService:
    return UserService(UserRepository(db))


def get_tile_cache_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TileCacheService:
    return TileCacheService(CachedTileRepository(db))


def get_viewshed_service(db: Annotated[AsyncSession, Depends(get_db)]) -> ViewshedService:
    tile_service = S3TileService(CachedTileRepository(db), settings)
    return ViewshedService(tile_service, ViewshedProcessor(settings), settings)


async def verify_csrf_token(
    request: Request,
    csrf_token: Annotated[str | None, Form()] = None,
) -> None:
    if not csrf_token_is_valid(request.cookies.get(CSRF_COOKIE_NAME), csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
