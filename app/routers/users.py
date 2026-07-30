from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr

from app.dependencies import (
    get_current_active_user,
    get_current_admin_user,
    get_user_service,
)
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.users import UserService

router = APIRouter(tags=["User management"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/manage-users", response_class=HTMLResponse, include_in_schema=False)
async def manage_users_page(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[UserService, Depends(get_user_service)],
) -> HTMLResponse:
    users = await service.repository.list_all() if current_user.is_admin else [current_user]
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={"current_user": current_user, "users": users},
    )


@router.post("/users/create", response_class=RedirectResponse, include_in_schema=False)
async def create_user(
    email: Annotated[EmailStr, Form()],
    password: Annotated[str, Form(min_length=8, max_length=128)],
    service: Annotated[UserService, Depends(get_user_service)],
    _current_user: Annotated[User, Depends(get_current_admin_user)],
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
) -> RedirectResponse:
    await service.delete(user_id, current_user)
    return RedirectResponse(url="/manage-users", status_code=status.HTTP_303_SEE_OTHER)
