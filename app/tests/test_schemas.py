import pytest
from pydantic import ValidationError

from app.schemas.viewshed import ViewshedRequest


def test_viewshed_request_uses_geojson_coordinate_order() -> None:
    request = ViewshedRequest(
        observer_coordinates=(174.2, -39.0),
        observer_height_agl_m=30,
        radius_m=1000,
    )

    assert request.longitude == 174.2
    assert request.latitude == -39.0
    assert request.target_height_agl_m == 0


@pytest.mark.parametrize(
    "coordinates",
    [(181, 0), (-181, 0), (0, 91), (0, -91)],
)
def test_viewshed_request_rejects_invalid_coordinates(
    coordinates: tuple[float, float],
) -> None:
    with pytest.raises(ValidationError):
        ViewshedRequest(
            observer_coordinates=coordinates,
            observer_height_agl_m=30,
            radius_m=1000,
        )


def test_viewshed_request_rejects_radius_over_100_km() -> None:
    with pytest.raises(ValidationError):
        ViewshedRequest(
            observer_coordinates=(174.2, -39.0),
            observer_height_agl_m=30,
            radius_m=100_001,
        )
