from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr

from app.config import settings
from app.dependencies import (
    get_current_active_user,
    get_current_admin_user,
    get_user_service,
    verify_csrf_token,
)
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.auth import CSRF_COOKIE_NAME, create_csrf_token
from app.services.users import UserService

router = APIRouter(tags=["User management"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/manage-users", response_class=HTMLResponse, include_in_schema=False)
async def manage_users_page(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[UserService, Depends(get_user_service)],
) -> HTMLResponse:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "csrf_token": csrf_token,
            "current_user": current_user,
            "users": await service.list_visible_to(current_user),
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


@router.post("/users/create", response_class=RedirectResponse, include_in_schema=False)
async def create_user(
    email: Annotated[EmailStr, Form()],
    password: Annotated[str, Form(min_length=8, max_length=128)],
    service: Annotated[UserService, Depends(get_user_service)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
    is_admin: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    await service.create(UserCreate(email=email, password=password, is_admin=is_admin))
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/users/{user_id}/regen-token",
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def regenerate_token(
    user_id: int,
    service: Annotated[UserService, Depends(get_user_service)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    await service.regenerate_token(user_id)
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/users/{user_id}/toggle-active",
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def toggle_active(
    user_id: int,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    await service.toggle_active(user_id, current_user)
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/users/{user_id}/toggle-admin",
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def toggle_admin(
    user_id: int,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    await service.toggle_admin(user_id, current_user)
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/users/{user_id}/delete",
    response_class=RedirectResponse,
    include_in_schema=False,
)
async def delete_user(
    user_id: int,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(get_current_admin_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    await service.delete(user_id, current_user)
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)
