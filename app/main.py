import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.database import ensure_data_directories
from app.dependencies import get_current_user
from app.exceptions import (
    ApplicationError,
    DemCoverageError,
    DuplicateUserError,
    InvalidUserOperationError,
    RadiusLimitError,
    TileDownloadError,
    UserNotFoundError,
    ViewshedProcessingError,
)
from app.models.user import User
from app.routers import auth, tile_cache, users, viewsheds
from app.schemas.health import HealthResponse

# Uvicorn configures this logger with the same handler used for its server diagnostics, ensuring
# application errors are visible beside the access log in container output.
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    ensure_data_directories()
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="Copernicus GLO-30 Viewshed API",
        version="1.0.0",
        description="Authenticated terrain viewsheds generated from Copernicus GLO-30 DGED.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_parameters={"persistAuthorization": True},
    )
    application.include_router(auth.router)
    application.include_router(users.router)
    application.include_router(tile_cache.router)
    application.include_router(viewsheds.router)
    register_exception_handlers(application)

    @application.get("/health", response_model=HealthResponse, tags=["Operations"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @application.get("/docs", include_in_schema=False)
    async def custom_swagger_ui(
        current_user: Annotated[User | None, Depends(get_current_user)],
    ) -> Response:
        if current_user is None:
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

        html = get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{application.title} - Swagger UI",
            swagger_ui_parameters=application.swagger_ui_parameters,
        )
        bearer_token = current_user.bearer_token
        injection = ""
        if bearer_token:
            encoded_token = json.dumps(bearer_token)
            injection = f"""
<script>
window.addEventListener('load', function () {{
  const timer = setInterval(function () {{
    if (window.ui) {{
      clearInterval(timer);
      window.ui.preauthorizeApiKey('BearerAuth', {encoded_token});
    }}
  }}, 100);
}});
</script>
"""
        back_link = (
            '<p style="position:fixed;top:8px;left:8px;z-index:9999"><a href="/">Home</a></p>'
        )
        return HTMLResponse(bytes(html.body).decode("utf-8") + injection + back_link)

    @application.get("/openapi.json", include_in_schema=False)
    async def openapi_document(
        current_user: Annotated[User | None, Depends(get_current_user)],
    ) -> JSONResponse:
        if current_user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        return JSONResponse(schema)

    return application


def register_exception_handlers(application: FastAPI) -> None:
    mappings: list[tuple[type[Exception], int]] = [
        (DuplicateUserError, status.HTTP_409_CONFLICT),
        (UserNotFoundError, status.HTTP_404_NOT_FOUND),
        (InvalidUserOperationError, status.HTTP_400_BAD_REQUEST),
        (RadiusLimitError, status.HTTP_422_UNPROCESSABLE_ENTITY),
        (DemCoverageError, status.HTTP_422_UNPROCESSABLE_ENTITY),
        (TileDownloadError, status.HTTP_502_BAD_GATEWAY),
        (ViewshedProcessingError, status.HTTP_500_INTERNAL_SERVER_ERROR),
    ]

    for exception_type, status_code in mappings:

        async def handler(
            request: Request,
            exception: Exception,
            response_status: int = status_code,
        ) -> JSONResponse:
            log_detail = (
                exception.log_detail
                if isinstance(exception, ApplicationError) and exception.log_detail
                else None
            )
            diagnostic_suffix = f"; {log_detail}" if log_detail else ""
            logger.log(
                logging.ERROR if response_status >= 500 else logging.WARNING,
                "Application error: %s %s returned %d (%s): %s%s",
                request.method,
                request.url.path,
                response_status,
                type(exception).__name__,
                exception,
                diagnostic_suffix,
                exc_info=(type(exception), exception, exception.__traceback__)
                if response_status >= 500
                else None,
            )
            return JSONResponse(status_code=response_status, content={"detail": str(exception)})

        application.add_exception_handler(exception_type, handler)


app = create_app()
