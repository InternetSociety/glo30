# Elevations and area-horizon handover

## Implementation

The service adds two authenticated operations:

- `POST /api/v1/elevations` returns native cell elevations in input order, including duplicates.
- `POST /api/v1/area-horizon` returns `within_horizon`, `beyond_horizon`, or `indeterminate`, with coverage, evidence, and source provenance.

[README.md](../../README.md#native-point-elevations) describes the request and response contracts, defaults, source heights, and error semantics.
Protected OpenAPI documents both typed operations.
Both operations use the existing active-account dependency.

Native acquisition rejects cached synthetic terrain and preserves upstream errors.
Viewsheds retain their missing-neighbor zero fallback.
New synthetic files use `_ZERO.tif`, separate from real `_DEM.tif` files.
Existing synthetic cache rows require no migration. Native requests replace them with actual sources when available.
No database migration or new dependency is required.

For polygon witnesses, AEQD radius equals WGS84 geodesic distance.
The processor minimizes this radius along original clipped cell edges rather than straight chords between projected vertices.
This keeps the witness on the licensed geometry and avoids a false nearby witness across the antipodal projection cut.
The vectorized search includes edge endpoints and exact interior-observer locations.
Forty refinement steps give submicrometer parameter resolution for native cell edges, subject to floating-point geodesic precision.

The cell limit counts deduplicated candidate windows, as approved during planning.
The deadline covers planning, tile acquisition, hashing, and evaluation.
The processor reads at most 4,096 raster cells per batch and checks cancellation between bounded operations.
When a source is absent, unavailable-cell counts use the documented native DGED grid for its latitude band.
The cell budget is an upper bound, not a throughput guarantee.
A review probe took about 0.36 seconds per 4,096 native cells, so the deadline can apply below the cell budget.

A shared validation-handler correction preserves HTTP 422 for NaN and Infinity input.
Previously, serialization of those invalid values could turn the validation response into HTTP 500.
Finite-input validation responses retain their existing structure.

## Verification

Checks ran through Docker Compose on 16 November 2026:
The rebuilt application image uses CPython 3.14.10.

| Check | Result |
| --- | --- |
| Baseline offline suite | 76 passed, live integration excluded |
| Docker Compose application build | Passed |
| Full offline suite with coverage | 133 passed, 1 live integration skipped |
| Coverage | 95% overall, including test modules under the existing configuration |
| Ruff lint | Passed |
| Ruff format check | Passed |
| mypy | Passed for 35 source files |
| Handover prose lint | 0.77 findings per 100 words |

Synthetic rasters and fake acquisition isolate all new tests from Copernicus services.
Tests use the real isolated SQLite fixtures for authentication and cache integration.
The suite covers missing terrain, synthetic cache entries, geometry validation, datum-preserving sampling, provenance, limits, deadlines, and event-loop responsiveness.
It also covers horizon evidence, holes, disconnected shapes, narrow shapes, global bounds, native transforms, and antipodal geometry.
Existing viewshed regression tests pass.
Rasterio emits its existing affine-transform deprecation warning.

## Delivery status

The implementation remains an uncommitted working-tree change based on source revision `17cff060d61072c74f52c12d5d2c211b3cb54610`.
That revision identifies the baseline, not the new API implementation.
No deployment or live Copernicus request forms part of these checks.
No message was sent to reng.

Before reng Handover 07 starts, the deployment owner must record the feature commit and deployed service URL.
The owner must also test both README request examples with an authorized cookie/token-capable client against the deployed service.
These deployment checks remain pending. Credentials must stay out of the handover and command output.
