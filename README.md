# Copernicus GLO-30 Viewshed API

This FastAPI service creates terrain viewsheds from Copernicus GLO-30 DGED elevation data. It returns the visible area as a GeoJSON Feature and includes authenticated Swagger, user administration, and a tile-cache page.

Terrain data produced using Copernicus WorldDEM™-30 © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018, provided under COPERNICUS by the European Union and ESA; all rights reserved.

## How it Works

The service gets the Copernicus GLO-30 DGED GeoTIFF tiles required for a request. It first finds every one-degree geocell that intersects the requested circle. It uses a locally cached tile when one is available; otherwise, it finds the product through the Copernicus catalogue, downloads the DEM from the Copernicus Data Space Ecosystem S3 service, and records it in the SQLite cache.

All geographic input uses longitude and latitude in WGS 84 coordinates. The service reprojects the required DEM tiles into an Azimuthal Equidistant projection centred on the observer, so it can calculate distances and the output grid in metres.

`POST /api/v1/viewsheds` accepts an observer position, observer and target heights above ground level, and a radius. GDAL calculates the visible cells in the local raster. By default, it accounts for Earth curvature and uses an atmospheric-refraction coefficient of `1/7`.

The service polygonises the visible cells, simplifies the result to a configurable global vertex budget, and transforms it back to WGS 84. The response geometry is a GeoJSON Polygon or MultiPolygon. Its visible area and pixel count are calculated before simplification.

| Library | Use |
| --- | --- |
| Boto3 | Find and download Copernicus DGED tiles from S3. |
| HTTPX | Query the Copernicus catalogue for tile products. |
| Rasterio | Read, reproject, and polygonise DEM rasters. |
| GDAL | Calculate the terrain viewshed. |
| PyProj | Calculate geodesic coverage and create the local metre-based projection. |
| Shapely | Simplify and transform the output geometry. |

## Run the service

Docker Compose is the only supported application environment. CPython and all application tools run in the `app` container.

1. Copy `docker-compose.yml.example` to `docker-compose.yml`.
2. Copy `.env.example` to `.env`.
3. Set valid Copernicus Data Space Ecosystem S3 credentials and replace `secret_key` with a long, random value. Set `cookie_secure=true` for production HTTPS.
4. Build and start the service:

```bash
docker compose up --build
```

5. Create the first administrator:

```bash
docker compose run --rm app python manage_users.py create admin@example.com 'a-long-password'
```

The application is available at <http://localhost:8004>. The SQLite database and downloaded DEM tiles remain in the `glo30-data` Docker volume.

Do not install Python packages or run Python tools on the host. Declare dependencies in the repository, then rebuild the image.

## API

All application endpoints use the `/api/v1` prefix and require an active account:

| Method | Endpoint | Input | Result |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | None | Service health response. |
| `POST` | `/api/v1/viewsheds` | Observer, heights, and radius JSON body. | A visible-area GeoJSON Feature. |
| `POST` | `/api/v1/elevations` | Up to 1,000 longitude/latitude pairs. | Native elevations, availability, and source provenance. |
| `POST` | `/api/v1/area-horizon` | Observer ASL height and target geometry. | Radio-horizon outcome, evidence, coverage, and source provenance. |

For example:

```bash
curl -X POST \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8004/api/v1/viewsheds \
  --data '{
    "observer_coordinates": [174.2316077, -39.0035668],
    "observer_height_agl_m": 30,
    "target_height_agl_m": 0,
    "radius_m": 10000
  }'
```

`observer_coordinates` uses GeoJSON order: `[longitude, latitude]`. Heights and radius are metres. Observer and target heights must be from `0` through `10,000` metres. The radius must be greater than `0` and no greater than `max_radius_m`, which defaults to `100,000` metres.

The response has this shape:

```json
{
  "type": "Feature",
  "properties": {
    "observer_height_agl_m": 30,
    "observer_coordinates": [174.2316077, -39.0035668],
    "target_height_agl_m": 0,
    "radius_m": 10000,
    "dem": "Copernicus GLO-30 DGED",
    "visible_area_sq_km": 126.4,
    "visible_pixel_count": 140472,
    "resolution_m": 30,
    "earth_curvature": true,
    "refraction_coefficient": 0.14285714285714285
  },
  "geometry": {
    "type": "MultiPolygon",
    "coordinates": []
  }
}
```

Send API credentials in `Authorization: Bearer TOKEN`. The application tries a persistent token first, then accepts an access-type JWT from `POST /token`. Inactive accounts receive HTTP 403.

### Error handling

The API reports a valid request whose observer coordinate has no DEM tile as HTTP 422, rather than an upstream gateway error. This lets clients receive the application response instead of a proxy-generated 502 page.

A request that intersects a geocell withheld from public distribution receives HTTP 422 with an explanation that the geography is not yet released. An observer tile that has no Copernicus catalogue product or DEM object also receives HTTP 422:

```json
{
  "detail": "The geography you have requested is not available from Copernicus GLO-30"
}
```

When the observer tile is available but an adjacent tile has no catalogue product or DEM object, the API treats that adjacent tile as zero elevation and continues the viewshed calculation. This assumption is intended for ocean cells and can overstate visibility if an unpublished cell contains terrain.

Actual Copernicus catalogue or S3 failures receive HTTP 502. Viewshed-processing failures receive HTTP 500. The configured `glo30_restricted_tile_ids` list contains the known unavailable geocells.

### Native point elevations

`POST /api/v1/elevations` accepts this JSON body:

```json
{"coordinates": [[174.75, -41.25], [174.76, -41.26]]}
```

Supply 1–1,000 finite WGS84 coordinate pairs in `[longitude, latitude]` order.
Longitude accepts -180 through 180. Latitude accepts -90 through 90.
`ELEVATION_MAX_BATCH_POINTS` can lower the batch limit.
The service preserves order and duplicates.
It samples the containing native raster cell with floor-based indexing, without interpolation.
An exact geocell boundary selects the eastern or northern geocell. Global bounds clamp to the outermost geocell.

The response contains `results` and `provenance`.
Each result contains `coordinates`, `status`, `elevation_m`, and `reason`.
Successful results have status `ok`, a floating-point elevation, and a null reason.
Missing, withheld, masked, or nodata terrain has status `unavailable`, a null elevation, and reason `dem_unavailable`.
Zero and negative elevations remain valid measurements.
This operation never substitutes synthetic zero tiles, including synthetic tiles already in the viewshed cache.

Copernicus GLO-30 is a digital surface model (DSM), which includes vegetation and buildings.
Its native heights use EGM2008 (EPSG:3855), in meters.
DGED uses point-registered rasters and latitude-dependent longitudinal spacing.
The service uses the raster's actual transform and does no vertical conversion.
Callers must supply compatible observer ASL heights. Ellipsoidal GNSS heights require an external conversion.
See the [Copernicus dataset specification](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM).

### Area radio horizon

`POST /api/v1/area-horizon` accepts this JSON body:

```json
{
  "observer_coordinates": [174.75, -41.25],
  "observer_antenna_height_asl_m": 300,
  "target_height_agl_m": 2,
  "effective_earth_radius_factor": 1.3333333333333333,
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[174.8, -41.3], [174.9, -41.3], [174.9, -41.2], [174.8, -41.2], [174.8, -41.3]]]
  }
}
```

Supply a valid, nonempty GeoJSON Polygon, MultiPolygon, Point, or MultiPoint geometry.
Features, self-intersections, unclosed rings, extra coordinate dimensions, and polygons crossing the antimeridian receive HTTP 422.
Separate polygon components on opposite sides of the antimeridian are permitted.
Observer coordinates obey the elevation coordinate rules.

Observer ASL height accepts -500 through 12,000 meters. The calculation and response clamp negative observer heights to zero.
Target AGL height defaults to 2 meters and accepts 0 through 3,000 meters.
The effective Earth-radius factor defaults to 4/3 and accepts values greater than zero through 10.
All numeric inputs must be finite. The response echoes effective inputs.

Processor v2 evaluates native cell centres inside the licensed geometry and samples its exterior and hole boundaries.
Boundary samples include native grid crossings and interval midpoints, with spacing no greater than one native cell.
Thus, narrow polygons and slivers retain candidates even when they contain no cell centre.
Each candidate uses its own cell elevation and WGS84 geodesic distance.
The polygon comparison includes a fixed 0.05 km tolerance, larger than half a native cell diagonal.
Point and MultiPoint geometries retain exact coordinates and a zero-tolerance comparison.

The processor applies this smooth-earth formula, with heights in meters and distances in kilometers:

```text
h1 = max(0, observer_antenna_height_asl_m)
h2 = max(0, native_elevation_m + target_height_agl_m)
coefficient = 4.12 * sqrt(effective_earth_radius_factor / (4/3))
horizon_km = coefficient * (sqrt(h1) + sqrt(h2))
tolerance_km = 0.05 for Polygon/MultiPolygon, otherwise 0
within = distance_km - horizon_km <= tolerance_km
```

This operation tests the radio horizon with terrain receiver heights. It does not test terrain obstructions or calculate a viewshed.

| Outcome | Meaning |
| --- | --- |
| `within_horizon` | At least one valid terrain location is within the horizon, even if other terrain is unavailable. |
| `beyond_horizon` | All required terrain is available and every location is beyond the horizon. |
| `indeterminate` | Missing terrain prevents a decision and no valid location proves `within_horizon`. The reason is `incomplete_dem`. |

The response includes `coverage_complete`, `evaluated_cells`, `unavailable_cells`, `evidence`, and `provenance`.
Polygon counts describe interior-centre cells plus boundary cells not already counted. Multiple boundary samples in one cell count once.
Point/MultiPoint counts describe exact targets, including duplicates.
For absent files, unavailable-cell counts use the documented DGED grid for that latitude band.
Evidence contains `coordinates`, `elevation_m`, `distance_km`, and `horizon_km` for the smallest `distance_km - horizon_km`.
Ascending longitude, then latitude, resolves equal margins. Evidence is null when no valid terrain exists.
The processor compares full floating-point values and does not round evidence before serialization.
Beyond-horizon results retain the global minimum-margin evidence among v2 candidates.
Polygon processing stops after a batch supplies a within-horizon witness.
Such results return the best candidate examined so far, `coverage_complete: false`, reason `stopped_at_witness`, and partial counts.
An observer inside or on the geometry receives an immediate zero-distance witness when its containing cell has usable terrain.
Observers in holes do not qualify for that shortcut. Unavailable observer terrain does not prevent a search for another valid witness.

Only area horizon treats an unpublished geocell as sea level when acquisition reports `catalogue_product_not_found` or `dem_object_not_found`.
This rule applies to all area-horizon geometry types. Those cells count as evaluated at 0 meters.
Provenance lists their IDs in `sea_level_assumed_tiles`, without synthetic source files or invented hashes.
Restricted tiles, published-tile nodata, and upstream failures retain their existing behavior.
Point elevations still return `unavailable` for unpublished geocells.

Both native operations return source provenance with the DEM name, EPSG:4326 horizontal CRS, and native-height processing description.
Each inspected source tile supplies `tile_id`, `product_id`, and the source DEM file's `sha256`.
`product_id` is the actual S3 product prefix containing the geocell. Direct-prefix deployments use their configured source prefix.
Tile entries are unique and sorted by tile ID.
Elevation provenance uses sampling `native_cell` and processor version `elevation-v1`.
Area-horizon provenance uses processor version `area-horizon-v2`.
Polygon/MultiPolygon sampling is `native_cell_centre`, with `tolerance_km: 0.05`.
Point/MultiPoint sampling is `native_cell`, with `tolerance_km: 0`.
Area-horizon responses always include `sea_level_assumed_tiles`, which is empty when no sea-level assumption contributed.

The area limit counts the union of candidate raster windows before sampling.
Thin or fragmented shapes can exceed this conservative budget even when they intersect fewer cells.
The processor checks nominal windows before downloads and checks actual raster headers before evaluation.
It reads bounded raster windows and processes at most 4,096 cells per batch, outside the async event loop.
Batch rectangles wholly inside or outside the geometry avoid per-cell geometry tests.
Boundary-crossing batches use vectorized point-in-polygon tests rather than cell clipping.
After one raster read, each batch's maximum valid elevation and conservative distance bound determine whether it can improve the evidence.
Batches that cannot improve it skip per-candidate distances but still count terrain availability.
Tiles and their batches start with their nearest distance bounds. Request logs include read, distance, and pruned batch counts.
The deadline includes planning, acquisition, hashing, and evaluation. Workers check cancellation between bounded operations.

Invalid input receives HTTP 422. A cell-budget failure receives HTTP 422 with code `area_horizon_limit`.
The processing deadline receives HTTP 504 with code `area_horizon_timeout`.
Catalogue, network, and authentication failures retain the existing HTTP 502/504 mappings.
These failures never become ordinary unavailable terrain or a `beyond_horizon` result.

Run the authenticated handover 02 acceptance probes against the local Compose service:

```bash
docker compose run --rm app python -m scripts.benchmark_area_horizon
```

The command uses an existing active account and keeps its temporary JWT in memory.
It prints the three elapsed times, complete responses, and the served OpenAPI document's SHA-256 hash.
It checks the v2 processor, expected outcomes, and the warm Cook Strait request's 30-second limit.
See [handover 02 results](docs/handovers/area-horizon-performance.md) for measurements and changed regression tests.

## Configuration

Copy `.env.example` to `.env`. Change values there, then restart the app container. `app/config.py` defines the settings, types, validation, and defaults. Environment variables override those defaults.

| Setting | Default | Purpose |
| --- | ---: | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:////app/data/app.db` | SQLAlchemy database connection URL. |
| `TILE_CACHE_DIR` | `/app/data/tiles` | Directory for downloaded DGED GeoTIFF tiles. |
| `TILE_CACHE_EXPIRY_DAYS` | `30` | Days before an unused cached tile expires. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Required | Copernicus Data Space Ecosystem S3 credentials. |
| `S3_HOST_BASE` | `eodata.dataspace.copernicus.eu` | Copernicus S3 endpoint host. |
| `S3_BUCKET_NAME` | `eodata` | Copernicus S3 bucket name. |
| `GLO30_S3_PREFIX` | Empty | Optional direct S3 prefix that bypasses catalogue discovery. |
| `MAX_RADIUS_M` | `100000` | Largest accepted viewshed radius in metres. |
| `ELEVATION_MAX_BATCH_POINTS` | `1000` | Native elevation batch limit, from 1 through 1,000. |
| `AREA_HORIZON_MAX_CELLS` | `25000000` | Conservative candidate-cell budget for area horizon. |
| `AREA_HORIZON_TIMEOUT_SECONDS` | `300` | Total area-horizon deadline in seconds. |
| `DEM_RESOLUTION_M` | `30` | Working-raster cell size in metres. |
| `DEM_RESAMPLING_METHOD` | `bilinear` | Elevation interpolation for the working grid. |
| `GEOMETRY_VERTICES_PER_SQ_KM` | `100` | Vertex budget density for the output geometry. |
| `GEOMETRY_MIN_VERTEX_BUDGET` | `8` | Smallest global geometry vertex budget. |
| `GEOMETRY_MAX_VERTEX_BUDGET` | `10000` | Largest global geometry vertex budget. |
| `SECRET_KEY` | Required | Secret used to sign JWTs and CSRF tokens. Use a long random value. |
| `COOKIE_SECURE` | `false` | Send cookies only over HTTPS when `true`. |
| `SMTP_ENABLED` | `false` | Enable password-reset email delivery. |
| `SMTP_HOST` / `SMTP_FROM` | Empty | Required SMTP host and sender when email is enabled. |

`GLO30_S3_PREFIX` must use this direct-geocell layout:

```text
<glo30_s3_prefix>/
  Copernicus_DSM_10_<geocell>/DEM/Copernicus_DSM_10_<geocell>_DEM.tif
```

Lower `DEM_RESOLUTION_M` values make a finer working raster, with substantially greater memory and compute cost. Values below GLO-30's native detail interpolate existing terrain rather than adding measurements. Increase `GEOMETRY_VERTICES_PER_SQ_KM` to retain more curved edges and output detail. The complete output-shape controls are documented with their defaults in `app/config.py`.

Running the service requires a Copernicus Data Space Ecosystem account registered for Copernicus Contributing Missions access. Generate S3 credentials through the Copernicus Data Space Ecosystem portal.

## Authentication and UI

Open `/` and sign in with an administrator account. Authenticated users can access these pages:

- `/docs` for Swagger
- `/app-docs` for this guide
- `/manage-users` for their visible account information
- `/tile-cache` for the current GLO-30 tile inventory

Administrators manage users. API users get a random persistent bearer token; administrators never get one. `/token` exchanges a valid email and password for a short-lived JWT. Browser sessions use a separate typed JWT in an HTTP-only cookie.

Use `/forgot-password` to request a reset code. When SMTP delivery is enabled, the service emails a one-time code. The code expires after 30 minutes. Paste it into `/reset-password`. The acknowledgement is the same whether or not an eligible account exists.

## Database and first administrator

The container applies Alembic migrations when it starts. Use these commands for explicit migration work:

```bash
docker compose run --rm app alembic upgrade head
docker compose run --rm app alembic revision --autogenerate -m "description"
```

Create or remove an account with the user-management command:

```bash
docker compose run --rm app python manage_users.py create admin@example.com 'a-long-password'
docker compose run --rm app python manage_users.py remove user@example.com
```

The command does not print a password, password hash, JWT, or persistent token.

## Checks

Run every check through Compose:

```bash
docker compose run --rm app pytest
docker compose run --rm app pytest --cov=app
docker compose run --rm app ruff check .
docker compose run --rm app ruff format --check .
docker compose run --rm app mypy app/
```

Tests use a separate SQLite database. Rollback transactions isolate database changes.

## Structure

```text
app/
├── main.py
├── config.py
├── database.py
├── dependencies.py
├── models/
├── schemas/
├── routers/
├── services/
├── repositories/
└── templates/
tests/
├── conftest.py
├── test_routers/
└── test_services/
migrations/
```

## Citation

Copernicus DEM GLO-30 (DGED). European Space Agency (ESA) and the Copernicus Programme. Digital Surface Model (DSM), 30 m global resolution. DOI: 10.5270/ESA-c5d3d65.
