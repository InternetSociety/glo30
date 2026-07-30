import asyncio
from pathlib import Path
from typing import Annotated

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user, get_current_user
from app.models.user import User
from app.repositories.users import UserRepository
from app.schemas.auth import TokenResponse
from app.services.auth import create_access_token, verify_password
from app.services.users import UserService

router = APIRouter(tags=["Authentication and UI"])
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})


@router.post("/login", response_class=RedirectResponse, include_in_schema=False)
async def login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    repository = UserRepository(db)
    user = await repository.get_by_email(username.lower())
    if user is None or not verify_password(password, user.password_hash):
        return RedirectResponse(
            url="/?error=invalid_credentials",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not user.is_active:
        return RedirectResponse(url="/?error=inactive", status_code=status.HTTP_303_SEE_OTHER)

    await UserService(repository).mark_login(user)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=create_access_token(user.email, settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    return response


@router.post("/token", response_model=TokenResponse)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    repository = UserRepository(db)
    user = await repository.get_by_email(form_data.username.lower())
    if user is None or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    await UserService(repository).mark_login(user)
    return TokenResponse(access_token=create_access_token(user.email, settings))


@router.post("/logout", response_class=RedirectResponse, include_in_schema=False)
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token", path="/")
    return response


@router.get("/app-docs", response_class=HTMLResponse, include_in_schema=False)
async def application_documentation(
    request: Request,
    _current_user: Annotated[User, Depends(get_current_active_user)],
) -> HTMLResponse:
    readme = await asyncio.to_thread(Path("README.md").read_text, encoding="utf-8")
    content = markdown.markdown(readme, extensions=["fenced_code", "tables"])
    return templates.TemplateResponse(
        request=request,
        name="app_docs.html",
        context={"content": content},
    )
