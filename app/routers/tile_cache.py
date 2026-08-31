from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.dependencies import get_current_active_user, get_tile_cache_service
from app.models.user import User
from app.services.auth import CSRF_COOKIE_NAME, create_csrf_token
from app.services.tile_cache import TileCacheService

router = APIRouter(tags=["Tile cache"])
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


@router.get("/tile-cache", response_class=HTMLResponse, include_in_schema=False)
async def tile_cache_page(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[TileCacheService, Depends(get_tile_cache_service)],
) -> HTMLResponse:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="tile_cache.html",
        context={
            "current_user": current_user,
            "csrf_token": csrf_token,
            "tiles": await service.list_tiles(),
        },
    )
    if CSRF_COOKIE_NAME not in request.cookies:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.access_token_expire_minutes * 60,
            path="/",
        )
    return response
