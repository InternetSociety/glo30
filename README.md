# Copernicus GLO-30 Viewshed API

This FastAPI service returns a terrain viewshed as a GeoJSON `Feature`. It downloads the
required Copernicus GLO-30 DGED tiles from the Copernicus Data Space Ecosystem S3 service,
caches them locally, and performs each calculation in a local metre-based projection.

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
simplification. The output geometry budget is `ceil(visible_area_sq_km × 10)` vertices, with a
minimum of four vertices so that very small results can still form a valid polygon. Small
components may collapse during simplification when that is necessary to meet the budget.

## Viewshed method

For every request the service:

1. Constructs the geodesic bounds of the requested circle and identifies all intersecting
   one-degree GLO-30 geocells.
2. Downloads missing DGED GeoTIFFs from S3 and records them in the SQLite tile cache.
3. Reprojects the required data into an Azimuthal Equidistant CRS centred on the observer,
   using a 30 m output grid.
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
