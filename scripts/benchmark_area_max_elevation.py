"""Run authenticated handover 03 probes without printing account credentials."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import perf_counter

import httpx
from pyproj import Geod
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import User
from app.services.auth import create_access_token


async def main(base_url: str) -> None:
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.is_active.is_(True)).order_by(User.id).limit(1)
        )
        if user is None:
            raise RuntimeError("An existing active account is required for authenticated probes")
        token = create_access_token(user.email, settings)
    results = {"service": base_url, "checked_at": datetime.now(UTC).isoformat(), "cases": []}
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=360
    ) as client:
        schema = await client.get("/openapi.json")
        schema.raise_for_status()
        if "/api/v1/area-max-elevation" not in schema.json()["paths"]:
            raise RuntimeError("Service has not loaded the area maximum endpoint")
        results["openapi_sha256"] = hashlib.sha256(schema.content).hexdigest()
        for name, bounds in [
            ("mount_pirongia", (175.05, -38.03, 175.15, -37.95)),
            ("lower_south_island", (166.4, -46.7, 169.0, -44.3)),
        ]:
            west, south, east, north = bounds
            ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
            square_m, _ = Geod(ellps="WGS84").polygon_area_perimeter(
                [p[0] for p in ring], [p[1] for p in ring]
            )
            started = perf_counter()
            response = await client.post(
                "/api/v1/area-max-elevation",
                json={"geometry": {"type": "Polygon", "coordinates": [ring]}},
            )
            result = {
                "name": name,
                "bounds": bounds,
                "geodesic_area_sq_km": abs(square_m) / 1_000_000,
                "elapsed_seconds": perf_counter() - started,
                "http_status": response.status_code,
                "response": response.json(),
            }
            results["cases"].append(result)
            if response.status_code != 200 or response.json().get("status") != "ok":
                print(json.dumps(results, indent=2))
                raise RuntimeError(f"Acceptance probe failed: {name}")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://app:8004")
    asyncio.run(main(parser.parse_args().base_url))
