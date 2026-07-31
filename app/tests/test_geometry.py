from shapely.geometry import MultiPolygon, Point, Polygon

from app.services.viewshed import simplify_to_vertex_budget, vertex_count


def test_simplification_meets_vertex_budget() -> None:
    detailed_circle = Point(0, 0).buffer(1000, quad_segs=64)

    simplified = simplify_to_vertex_budget(
        detailed_circle,
        vertex_budget=10,
        max_tolerance=2000,
        search_iterations=40,
        preserve_topology=False,
    )

    assert not simplified.is_empty
    assert vertex_count(simplified) <= 10


def test_vertex_count_includes_holes_and_multipolygon_parts() -> None:
    polygon_with_hole = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        holes=[[(2, 2), (4, 2), (4, 4), (2, 4)]],
    )
    geometry = MultiPolygon([polygon_with_hole, Point(20, 20).buffer(1, quad_segs=1)])

    assert vertex_count(geometry) == 12
