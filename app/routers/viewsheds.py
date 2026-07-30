from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies import get_current_active_user, get_viewshed_service
from app.models.user import User
from app.schemas.viewshed import GeoJSONFeature, ViewshedRequest
from app.services.viewshed import ViewshedService

router = APIRouter(prefix="/api/v1", tags=["Viewsheds"])


@router.post(
    "/viewsheds",
    response_model=GeoJSONFeature,
    status_code=status.HTTP_200_OK,
    summary="Calculate a GLO-30 terrain viewshed",
)
async def create_viewshed(
    request: ViewshedRequest,
    _current_user: Annotated[User, Depends(get_current_active_user)],
    service: Annotated[ViewshedService, Depends(get_viewshed_service)],
) -> GeoJSONFeature:
    return await service.create(request)
