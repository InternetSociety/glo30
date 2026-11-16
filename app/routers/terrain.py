from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import (
    get_area_horizon_service,
    get_current_active_user,
    get_elevation_service,
)
from app.models.user import User
from app.schemas.terrain import (
    AreaHorizonRequest,
    AreaHorizonResponse,
    ElevationRequest,
    ElevationResponse,
)
from app.services.area_horizon import AreaHorizonService
from app.services.elevations import ElevationService

router = APIRouter(prefix="/api/v1", tags=["Terrain"])


@router.post(
    "/elevations", response_model=ElevationResponse, summary="Sample native GLO-30 elevations"
)
async def elevations(
    request: ElevationRequest,
    _current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[ElevationService, Depends(get_elevation_service)],
) -> ElevationResponse:
    return await service.create(request)


@router.post(
    "/area-horizon",
    response_model=AreaHorizonResponse,
    summary="Test a licensed area against the terrain-height radio horizon",
)
async def area_horizon(
    request: AreaHorizonRequest,
    _current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[AreaHorizonService, Depends(get_area_horizon_service)],
) -> AreaHorizonResponse:
    return await service.create(request)
