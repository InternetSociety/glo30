import logging
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.dependencies import get_viewshed_service
from app.exceptions import (
    CopernicusConfigurationError,
    CopernicusTimeoutError,
    DemCoverageError,
    TileDownloadError,
)
from app.main import app
from app.models.user import User
from app.schemas.viewshed import (
    GeoJSONFeature,
    GeoJSONGeometry,
    ViewshedProperties,
    ViewshedRequest,
)
from app.services.s3_tiles import (
    COPERNICUS_CONFIGURATION_MESSAGE,
    COPERNICUS_TIMEOUT_MESSAGE,
    GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_CODE,
    GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_MESSAGE,
    GLO30_TILE_UNAVAILABLE_CODE,
    GLO30_TILE_UNAVAILABLE_MESSAGE,
)


class FakeViewshedService:
    async def create(self, request: ViewshedRequest) -> GeoJSONFeature:
        return GeoJSONFeature(
            properties=ViewshedProperties(
                observer_height_agl_m=request.observer_height_agl_m,
                observer_coordinates=request.observer_coordinates,
                target_height_agl_m=request.target_height_agl_m,
                radius_m=request.radius_m,
                visible_area_sq_km=0.9,
                visible_pixel_count=1000,
                resolution_m=30,
                earth_curvature=True,
                refraction_coefficient=1 / 7,
            ),
            geometry=GeoJSONGeometry(
                type="Polygon",
                coordinates=[
                    [
                        [174.0, -39.0],
                        [174.1, -39.0],
                        [174.1, -39.1],
                        [174.0, -39.0],
                    ]
                ],
            ),
        )


REQUEST_BODY = {
    "observer_coordinates": [174.0, -39.0],
    "observer_height_agl_m": 30,
    "target_height_agl_m": 0,
    "radius_m": 1000,
}
TILE_ID = "Copernicus_DSM_10_S08_00_E132_00"


class CoverageErrorViewshedService:
    def __init__(self, reason: str, log_detail: str | None = None) -> None:
        self.reason = reason
        self.log_detail = log_detail

    async def create(self, _request: ViewshedRequest) -> GeoJSONFeature:
        raise DemCoverageError(
            GLO30_TILE_UNAVAILABLE_MESSAGE,
            code=GLO30_TILE_UNAVAILABLE_CODE,
            context={"tile_id": TILE_ID, "reason": self.reason},
            log_detail=self.log_detail,
        )


class CopernicusErrorViewshedService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create(self, _request: ViewshedRequest) -> GeoJSONFeature:
        raise self.error


class UnexpectedErrorViewshedService:
    async def create(self, _request: ViewshedRequest) -> GeoJSONFeature:
        raise RuntimeError("GDAL failed while processing internal DEM path")


@pytest.mark.asyncio
async def test_viewshed_requires_authentication(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        response = await client.post("/api/v1/viewsheds", json=REQUEST_BODY)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert caplog.record_tuples[-1] == (
        "uvicorn.error",
        logging.WARNING,
        "API request failed: POST /api/v1/viewsheds returned 401 "
        "(HTTPException): detail=Not authenticated",
    )


@pytest.mark.asyncio
async def test_viewshed_returns_typed_geojson(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
) -> None:
    await create_test_user()
    app.dependency_overrides[get_viewshed_service] = lambda: FakeViewshedService()

    response = await client.post(
        "/api/v1/viewsheds",
        json=REQUEST_BODY,
        headers={"Authorization": "Bearer test-bearer-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "Feature"
    assert payload["properties"]["dem"] == "Copernicus GLO-30 DGED"
    assert payload["properties"]["observer_coordinates"] == [174.0, -39.0]
    assert payload["geometry"]["type"] == "Polygon"


@pytest.mark.asyncio
async def test_viewshed_validation_failure_has_specific_safe_log(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_test_user()
    invalid_request = {**REQUEST_BODY, "observer_coordinates": [181, -39]}

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        response = await client.post(
            "/api/v1/viewsheds",
            json=invalid_request,
            headers={"Authorization": "Bearer test-bearer-token"},
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "value_error"
    assert "body.observer_coordinates" in caplog.record_tuples[-1][2]
    assert "longitude must be between -180 and 180" in caplog.record_tuples[-1][2]
    assert "[181, -39]" not in caplog.record_tuples[-1][2]


@pytest.mark.parametrize(
    "reason",
    ["catalogue_product_not_found", "dem_object_not_found", "tile_restricted"],
)
@pytest.mark.asyncio
async def test_viewshed_coverage_error_is_returned_as_client_error(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_test_user()
    log_detail = (
        "S3 object internal/prefix/tile_DEM.tif failed with "
        "credential-value and InternalDiscoveryError"
    )
    app.dependency_overrides[get_viewshed_service] = lambda: CoverageErrorViewshedService(
        reason,
        log_detail,
    )

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        response = await client.post(
            "/api/v1/viewsheds",
            json=REQUEST_BODY,
            headers={"Authorization": "Bearer test-bearer-token"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": GLO30_TILE_UNAVAILABLE_MESSAGE,
        "code": GLO30_TILE_UNAVAILABLE_CODE,
        "context": {"tile_id": TILE_ID, "reason": reason},
    }
    assert "internal/prefix" not in response.text
    assert "credential-value" not in response.text
    assert "InternalDiscoveryError" not in response.text
    assert caplog.record_tuples[-1] == (
        "uvicorn.error",
        logging.WARNING,
        "API request failed: POST /api/v1/viewsheds returned 422 (DemCoverageError): "
        f"detail={GLO30_TILE_UNAVAILABLE_MESSAGE}; code={GLO30_TILE_UNAVAILABLE_CODE}; "
        f"context={{'tile_id': '{TILE_ID}', 'reason': '{reason}'}}; log_detail={log_detail}",
    )


@pytest.mark.asyncio
async def test_viewshed_catalogue_layout_error_returns_502(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_test_user()
    diagnostic = "Catalogue product used an unexpected Name and S3Path layout"
    error = TileDownloadError(
        GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_MESSAGE,
        code=GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_CODE,
        context={"tile_id": TILE_ID},
        log_detail=diagnostic,
    )
    app.dependency_overrides[get_viewshed_service] = lambda: CopernicusErrorViewshedService(error)

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        response = await client.post(
            "/api/v1/viewsheds",
            json=REQUEST_BODY,
            headers={"Authorization": "Bearer test-bearer-token"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_MESSAGE,
        "code": GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_CODE,
        "context": {"tile_id": TILE_ID},
    }
    assert diagnostic in caplog.record_tuples[-1][2]


@pytest.mark.asyncio
async def test_unexpected_viewshed_error_is_detailed_only_in_server_log(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_test_user()
    app.dependency_overrides[get_viewshed_service] = lambda: UnexpectedErrorViewshedService()

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        response = await client.post(
            "/api/v1/viewsheds",
            json=REQUEST_BODY,
            headers={"Authorization": "Bearer test-bearer-token"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "GDAL failed" not in response.text
    assert "RuntimeError" in caplog.record_tuples[-1][2]
    assert "GDAL failed while processing internal DEM path" in caplog.record_tuples[-1][2]


@pytest.mark.parametrize(
    ("error", "status_code", "detail", "diagnostic"),
    [
        (
            CopernicusTimeoutError(
                COPERNICUS_TIMEOUT_MESSAGE,
                log_detail="Copernicus catalogue timed out (ReadTimeout)",
            ),
            504,
            COPERNICUS_TIMEOUT_MESSAGE,
            "Copernicus catalogue timed out (ReadTimeout)",
        ),
        (
            CopernicusConfigurationError(
                COPERNICUS_CONFIGURATION_MESSAGE,
                log_detail="Copernicus S3 rejected access (HTTP 403, code=InvalidAccessKeyId)",
            ),
            502,
            COPERNICUS_CONFIGURATION_MESSAGE,
            "Copernicus S3 rejected access (HTTP 403, code=InvalidAccessKeyId)",
        ),
    ],
)
@pytest.mark.asyncio
async def test_viewshed_copernicus_errors_return_safe_detail_and_explicit_logs(
    client: AsyncClient,
    create_test_user: Callable[..., Awaitable[User]],
    error: Exception,
    status_code: int,
    detail: str,
    diagnostic: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_test_user()
    app.dependency_overrides[get_viewshed_service] = lambda: CopernicusErrorViewshedService(error)

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        response = await client.post(
            "/api/v1/viewsheds",
            json=REQUEST_BODY,
            headers={"Authorization": "Bearer test-bearer-token"},
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert diagnostic in caplog.record_tuples[-1][2]
    if isinstance(error, CopernicusConfigurationError):
        assert "InvalidAccessKeyId" not in response.text
