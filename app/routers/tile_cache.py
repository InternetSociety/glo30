from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models.user import User
from app.repositories.cached_tiles import CachedTileRepository

router = APIRouter(tags=["Tile cache"])
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


@router.get("/tile-cache", response_class=HTMLResponse, include_in_schema=False)
async def tile_cache_page(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    tiles = await CachedTileRepository(db).list_all()
    return templates.TemplateResponse(
        request=request,
        name="tile_cache.html",
        context={"current_user": current_user, "tiles": tiles},
    )
