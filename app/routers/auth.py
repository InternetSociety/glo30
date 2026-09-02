import asyncio
from pathlib import Path
from typing import Annotated

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr

from app.config import settings
from app.dependencies import (
    get_current_active_user,
    get_current_user,
    get_user_service,
    verify_csrf_token,
)
from app.exceptions import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidPasswordResetCodeError,
)
from app.models.user import User
from app.schemas.auth import TokenResponse, UserCredentials
from app.services.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_access_token,
    create_csrf_token,
)
from app.services.users import UserService

router = APIRouter(tags=["Authentication and UI"])
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
) -> HTMLResponse:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"current_user": user, "csrf_token": csrf_token},
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


@router.get("/forgot-password", response_class=HTMLResponse, include_in_schema=False)
async def forgot_password_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user)],
) -> HTMLResponse:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"current_user": current_user, "csrf_token": csrf_token},
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


@router.post("/forgot-password", response_class=RedirectResponse, include_in_schema=False)
async def forgot_password(
    email: Annotated[EmailStr, Form()],
    service: Annotated[UserService, Depends(get_user_service)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    await service.request_password_reset(str(email))
    return RedirectResponse(
        url="/forgot-password?sent=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
async def reset_password_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user)],
) -> HTMLResponse:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={"current_user": current_user, "csrf_token": csrf_token},
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


@router.post(
    "/reset-password",
    response_class=RedirectResponse,
    response_model=None,
    include_in_schema=False,
)
async def reset_password(
    request: Request,
    reset_code: Annotated[str, Form()],
    new_password: Annotated[str, Form(min_length=8, max_length=128)],
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User | None, Depends(get_current_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> HTMLResponse | RedirectResponse:
    try:
        await service.reset_password(reset_code, new_password)
    except InvalidPasswordResetCodeError:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "current_user": current_user,
                "csrf_token": request.cookies.get(CSRF_COOKIE_NAME),
                "invalid_code": True,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/?reset=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/login", response_class=RedirectResponse, include_in_schema=False)
async def login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    service: Annotated[UserService, Depends(get_user_service)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    try:
        credentials = UserCredentials(email=username, password=password)
        user = await service.authenticate(str(credentials.email), credentials.password)
    except InvalidCredentialsError:
        return RedirectResponse(
            url="/?error=invalid_credentials",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except InactiveUserError:
        return RedirectResponse(url="/?error=inactive", status_code=status.HTTP_303_SEE_OTHER)

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_access_token(user.email, settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    csrf_token = create_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
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
    service: Annotated[UserService, Depends(get_user_service)],
) -> TokenResponse:
    credentials = UserCredentials(email=form_data.username, password=form_data.password)
    try:
        user = await service.authenticate(str(credentials.email), credentials.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        ) from exc
    return TokenResponse(access_token=create_access_token(user.email, settings))


@router.post("/logout", response_class=RedirectResponse, include_in_schema=False)
async def logout(
    _current_user: Annotated[User, Depends(get_current_active_user)],
    _csrf: Annotated[None, Depends(verify_csrf_token)],
) -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/app-docs", response_class=HTMLResponse, include_in_schema=False)
async def application_documentation(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> HTMLResponse:
    readme = await asyncio.to_thread(Path("README.md").read_text, encoding="utf-8")
    content = markdown.markdown(readme, extensions=["fenced_code", "tables"])
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or create_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="app_docs.html",
        context={
            "content": content,
            "csrf_token": csrf_token,
            "current_user": current_user,
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
