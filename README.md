# Copernicus GLO-30 Viewshed API

This FastAPI service returns a terrain viewshed as a GeoJSON `Feature`. It downloads the
required Copernicus GLO-30 DGED tiles from the Copernicus Data Space Ecosystem S3 service,
caches them locally, and performs each calculation in a local metre-based projection.
The supported runtime is CPython 3.14 in Docker Compose.

## Attribution

Terrain data produced using Copernicus WorldDEM™-30 © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018, provided under COPERNICUS by the European Union and ESA; all rights reserved.


## API

`POST /api/v1/viewsheds` requires a bearer token and a JSON body:

```json
{
  "observer_coordinates": [174.2316077, -39.0035668],
  "observer_height_agl_m": 30,
  "target_height_agl_m": 0,
  "radius_m": 10000
}
```

Coordinates use GeoJSON order: `[longitude, latitude]`. Heights and radius are metres. The
default maximum radius is 100,000 m and can be reduced with `max_radius_m`.

The response is a GeoJSON Polygon or MultiPolygon:

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

The visible area and pixel count are calculated from the binary 30 m raster before geometry
simplification. By default, the output geometry budget is
`ceil(visible_area_sq_km × geometry_vertices_per_sq_km)` vertices, clamped between the
`geometry_min_vertex_budget` floor and `geometry_max_vertex_budget` ceiling. Small components may
collapse during simplification when that is necessary to meet the budget.

### Error handling

A valid request whose required DEM tile is unavailable is reported as `422 Unprocessable Entity`,
not as an upstream gateway failure. This allows API clients, including clients reaching the service
through Cloudflare, to receive the application response instead of a proxy-generated `502` page.

If a request intersects a tile known to be withheld from public distribution, the response is:

```json
{
  "detail": "The geography you have requested is not yet released to the public. Please visit https://sentinels.copernicus.eu/-/copernicus-dem-30-metre-dataset-now-freely-available for more information"
}
```

If Copernicus has no GLO-30 catalogue product or DEM object for a tile that is not on the known
restricted list, the response is:

```json
{
  "detail": "The geography you have requested is not available from Copernicus GLO-30"
}
```

Actual failures while contacting the Copernicus catalogue or S3 service remain `502 Bad Gateway`
responses. Internal viewshed-processing failures remain `500 Internal Server Error` responses.
Handled application errors are also written to the Uvicorn error log with the request method, path,
status, exception type, and internal diagnostic context. For example:

```text
WARNING: Application error: POST /api/v1/viewsheds returned 422 (DemCoverageError): The geography you have requested is not available from Copernicus GLO-30; No GLO-30 catalogue product covers Copernicus_DSM_10_S40_00_E174_00
```

True `5xx` application errors include a traceback in the origin log.

#### Known unavailable tiles

The default `glo30_restricted_tile_ids` setting contains the following 25 unique geocells, covering
parts of Armenia and Azerbaijan. Requests whose radius intersects any of these tiles receive the
"not yet released to the public" response above. The compact code `N40E044`, for example,
corresponds to the full Copernicus identifier `Copernicus_DSM_10_N40_00_E044_00`.

- `N38E045`
- `N38E046`
- `N38E048`
- `N38E049`
- `N39E044`
- `N39E045`
- `N39E046`
- `N39E047`
- `N39E048`
- `N39E049`
- `N40E043`
- `N40E044`
- `N40E045`
- `N40E046`
- `N40E047`
- `N40E048`
- `N40E049`
- `N40E050`
- `N41E043`
- `N41E044`
- `N41E045`
- `N41E046`
- `N41E047`
- `N41E048`
- `N41E049`

### Output shape tuning

All tuning values are environment-backed settings documented beside their defaults in
`app/config.py`. The most useful controls are:

- `geometry_vertices_per_sq_km` (default `100`): increase this first to retain more curved edges
  and detail. It controls a global budget shared by all polygons and holes.
- `geometry_min_vertex_budget` (default `8`): raises the budget only for very small total visible
  areas; it is not a per-polygon minimum.
- `geometry_max_vertex_budget` (default `10000`): caps geometry complexity and response size for
  large visible areas.
- `dem_resolution_m` (default `30`): lower values create a finer working raster at substantially
  greater memory and compute cost. Values below GLO-30's native detail interpolate the source.
- `dem_resampling_method` (default `bilinear`): controls elevation interpolation onto that grid.
- `geometry_polygon_connectivity` and `geometry_simplification_preserve_topology`: control how
  diagonal cells, components, and holes survive polygonisation and simplification.

The remaining simplification search controls are normally left at their defaults. The
`coverage_boundary_sample_interval_degrees` setting only identifies source tiles and does not
change output polygon detail.


## Viewshed method

For every request the service:

1. Samples the requested circle at the configured coverage-boundary interval, constructs its
   geodesic bounds, and identifies all intersecting one-degree GLO-30 geocells.
2. Downloads missing DGED GeoTIFFs from S3 and records them in the SQLite tile cache.
3. Reprojects the required data into an Azimuthal Equidistant CRS centred on the observer,
   using the configured output grid (30 m by default).
4. Runs GDAL `gdal_viewshed` with the requested observer and target heights and maximum distance.
5. Applies Earth curvature using GDAL curvature coefficient `1 - 1/7`; the corresponding
   atmospheric refraction coefficient is `1/7`.
6. Polygonises visible cells, simplifies to the vertex budget, and transforms the result to
   EPSG:4326.

By default, the service locates each uncached geocell through the Copernicus catalogue, resolves
the corresponding object below the live `CCM/COP-DEM_GLO-30-DGED` S3 hierarchy, and stores the
resolved object key in SQLite. A deployment with a stable direct geocell hierarchy can bypass
catalogue discovery by setting `glo30_s3_prefix`; that prefix must use this layout:

```text
<glo30_s3_prefix>/
  Copernicus_DSM_10_<geocell>/DEM/Copernicus_DSM_10_<geocell>_DEM.tif
```

## Authentication and users

- Administrators sign into the web UI with email and password.
- Administrators create, activate, promote, and remove users at `/manage-users`.
- Each non-admin user receives a persistent bearer token. An administrator can regenerate it.
- JWT cookies authenticate web sessions. Persistent user tokens authenticate API requests.
- Every active user can inspect the current GLO-30 tile inventory at `/tile-cache`.
- `/docs` and `/openapi.json` are available only after sign-in. Swagger is pre-authorised with
  the signed-in regular user's bearer token.

Create the first administrator inside the Compose service:

```bash
docker compose run --rm app alembic upgrade head
docker compose run --rm app python manage_users.py create admin@example.com 'change-this-password'
```

Remove a user:

```bash
docker compose run --rm app python manage_users.py remove user@example.com
```

## Configuration

Copy the supplied examples to the untracked deployment files:

```text
docker-compose.yml.example -> docker-compose.yml
.env.example               -> .env
```

The existing `.env` names are supported directly by `app/config.py`:

```dotenv
s3_access_key = ...
s3_secret_key = ...
s3_host_base = eodata.dataspace.copernicus.eu
s3_host_bucket = eodata.dataspace.copernicus.eu
secret_key = a-long-random-deployment-secret
cookie_secure = true
```

`s3_host_bucket` is retained for compatibility with the supplied configuration; the actual S3
bucket name defaults to `eodata` and can be changed with `s3_bucket_name`.

s3 access and secret keys are provided by Compernicus.

Running the application requires a Copernicus Data Space Ecosystem (CDSE) account that is registered for Copernicus Contributing Missions (CCM) access. Account holders may generate s3 credentials through the Copernicus Data Portal. The application downloads and caches the required terrain tiles using the authenticated CDSE APIs. Information on registering for CCM access and the available download interfaces (Copernicus Browser, OData and S3) is available from the Copernicus Data Space Ecosystem documentation.

## Docker Compose operation

The application and all development tools run only through Docker Compose.

```bash
docker compose up --build
```

The service listens on `http://localhost:8004`. The Compose command applies migrations before
starting Uvicorn.

Run migrations explicitly:

```bash
docker compose run --rm app alembic upgrade head
```

Run verification:

```bash
docker compose run --rm app pytest
docker compose run --rm app pytest --cov=app
docker compose run --rm app ruff check .
docker compose run --rm app ruff format --check .
docker compose run --rm app mypy app/
```

Cached DEM files and the SQLite database are stored in the `glo30-data` volume. The default cache
expiry is 30 days from last use.

## Project layout

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
├── templates/
└── tests/
migrations/
Dockerfile
docker-compose.yml.example
```
## Citation

Copernicus DEM GLO-30 (DGED). European Space Agency (ESA) and the Copernicus Programme. Digital Surface Model (DSM), 30 m global resolution. DOI: 10.5270/ESA-c5d3d65.
