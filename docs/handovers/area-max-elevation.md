# Area maximum-elevation handover 03

## Result and live environment

`POST /api/v1/area-max-elevation` is implemented and serves authenticated requests in the local Docker Compose service at `http://app:8004`.
The host exposes the service on port 8004. No separate production deployment or reng edit forms part of this work.

The requested live probes returned:

| Area | Geodesic area | Valid cells | Unavailable cells | Maximum | Elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mount Pirongia, 175.05 to 175.15 / -38.03 to -37.95 | 78.003 km² | 104,331 | 0 | 953.553650 m | 0.374 s |
| Lower South Island, 166.4 to 169.0 / -46.7 to -44.3 | 54,195.487 km² | 80,888,411 | 0 | 2,894.486816 m | 61.121 s |

Both responses returned HTTP 200, `status: ok`, and `coverage_complete: true`.
Pirongia's maximum is close to the handover's approximate 959 m expectation, without rounding or vertical conversion.
Its selected cell centre is `[175.09777777777776, -37.992777777777775]`.
The lower South Island maximum is at `[168.7263888888889, -44.384166666666665]`.

The large request exceeds the required 30,000 km² acceptance size and completes under the default 300-second deadline.
Its elapsed time includes downloads of uncached tiles, source hashing, the full raster scan, and HTTP handling.
It uses eleven actual DEM files and one unpublished sea geocell, `Copernicus_DSM_10_S45_00_E166_00`.
That geocell appears in `sea_level_assumed_tiles`. Real source hashes appear in `tiles`.

[Complete live responses](area-max-elevation-probes.json) contain exact timings, coordinates, counts, product prefixes, and source hashes.
Run the same authenticated probes with:

```bash
docker compose run --rm app python -m scripts.benchmark_area_max_elevation
```

The command uses an existing active account and keeps its temporary JWT in memory.
SHA-256 of the exact OpenAPI response bytes served during these probes:

```text
0e8f1b5901bcda76f2e66b3ada424e608e346a13f3cf50290552a7d812b29f39
```

Source remains an uncommitted working-tree change based on `b9c9d780e00d965a12948ae17c628095bcab2a78`.
That revision identifies the baseline, not a feature commit containing this endpoint. No message was sent to reng.

## API and implementation

The request accepts a GeoJSON Polygon or MultiPolygon under the existing area-horizon validation rules.
Swagger supplies a complete Pirongia request example. Tests submit that published example unchanged and receive HTTP 200.

The service selects cells whose centres are inside the geometry, plus cells containing exterior or hole boundary samples.
Boundary sampling uses the area-horizon v2 grid crossings and midpoints, at no more than one native cell spacing.
Selected cells count once even when several samples or disconnected pieces use the same cell.
A boundary-selected cell's returned centre can lie outside the licensed shape, as required by the native-cell contract.

The maximum comes from valid native cell values. Zero and negative heights remain valid.
Ascending longitude, then latitude, resolves ties. The response returns that cell's centre rather than a boundary sample coordinate.
Real tile acquisition and provenance reuse the existing native DEM service.
Unpublished geocells are logical sea-level terrain. Restricted tiles, nodata, nonfinite values, and uncovered source areas remain unavailable.
No synthetic DEM file or invented source hash is created.

`status: ok` means at least one selected cell had a valid elevation.
`status: unavailable` means none did, with null elevation and coordinates.
`coverage_complete` is true exactly when `unavailable_cells` is zero.
With partial coverage, the maximum describes known cells and cannot prove the full area's maximum.
Reng must retain that coverage information when it stores the result.

The operation reads each selected 64 by 64 batch once, with no geodesic calculations or early termination.
Interior/exterior batch classification avoids unnecessary per-cell geometry tests.
Boundary batches reuse vectorized centre tests and boundary-cell selection.
A test rejects any call to the geodesic-bound helper during a maximum scan.

Independent settings are declared in `.env.example` and documented in the README:

| Setting | Default |
| --- | ---: |
| `AREA_MAX_ELEVATION_MAX_CELLS` | 250,000,000 |
| `AREA_MAX_ELEVATION_TIMEOUT_SECONDS` | 300 |

Nominal-grid and actual-header preflight checks use the new candidate-cell budget.
A budget failure returns HTTP 422 with code `area_max_elevation_limit`.
The total deadline includes acquisition and hashing and returns HTTP 504 with code `area_max_elevation_timeout`.
Raster work runs outside the event loop and checks deadlines and cancellation between bounded operations.
Upstream failures retain the existing HTTP 502/504 mappings.

The shared sampler accepts a null observer only for scans that do not need distance bounds.
The area-horizon path still supplies its observer and runs its original branch.
Polygon validation is shared without changing its rules. Viewshed, point-elevation, and area-horizon regression tests pass unchanged.
No migration or new dependency is required.

## Verification

Checks ran through Docker Compose with CPython 3.14.10:

| Check | Actual result |
| --- | --- |
| Docker Compose application build | Passed |
| Full offline pytest suite with coverage | 168 passed, 1 live integration skipped |
| Coverage under existing configuration, including tests | 96% |
| Ruff lint | Passed |
| Ruff format check | Passed |
| mypy | Passed for 36 application source files |
| Git whitespace check | Passed |

The dedicated suite covers deep interior maxima, higher cells outside the polygon, holes, slivers, and deterministic ties.
It also covers negative heights, nodata, nonfinite terrain, unpublished sea, restricted terrain, and wholly unavailable areas.
Authentication, independent limits, geometry rejection, the published Swagger example, acquisition timeout, and worker cancellation have regression tests.
The worker test also confirms that the event loop can serve an authenticated health request while raster work waits in a thread.
Review found no important correctness issues. Rasterio continues to emit its existing affine-transform deprecation warning.

The live results and OpenAPI hash above are ready for reng's integration check against this local service.
A separately deployed service must publish its own OpenAPI hash and source commit before the result is described as a production deployment.
