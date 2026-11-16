"""Run handover 02 probes against a Compose service without printing credentials."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import perf_counter

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import User
from app.services.auth import create_access_token

COOK_STRAIT = {
    "observer_coordinates": [175.0538158, -38.0435162],
    "observer_antenna_height_asl_m": 575,
    "target_height_agl_m": 2,
    "effective_earth_radius_factor": 4 / 3,
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [173.5930984, -40.8449815],
                [174.7788112, -40.8327585],
                [175.4006794, -41.6321608],
                [174.2006852, -41.6507727],
                [173.5930984, -40.8449815],
            ]
        ],
    },
}


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
        results["openapi_sha256"] = hashlib.sha256(schema.content).hexdigest()
        cases = [
            ("cook_strait_575m", COOK_STRAIT, "beyond_horizon"),
            (
                "cook_strait_12000m",
                {**COOK_STRAIT, "observer_antenna_height_asl_m": 12000},
                "within_horizon",
            ),
            (
                "point_readiness",
                {
                    "observer_coordinates": [174.2316077, -39.0035668],
                    "observer_antenna_height_asl_m": 300,
                    "target_height_agl_m": 2,
                    "effective_earth_radius_factor": 4 / 3,
                    "geometry": {"type": "Point", "coordinates": [174.2316077, -39.0035668]},
                },
                "within_horizon",
            ),
        ]
        for name, request, expected in cases:
            started = perf_counter()
            response = await client.post("/api/v1/area-horizon", json=request)
            result = {
                "name": name,
                "elapsed_seconds": perf_counter() - started,
                "http_status": response.status_code,
                "response": response.json(),
            }
            results["cases"].append(result)
            if (
                response.status_code != 200
                or response.json().get("outcome") != expected
                or response.json().get("provenance", {}).get("processor_version")
                != "area-horizon-v2"
            ):
                print(json.dumps(results, indent=2))
                raise RuntimeError(f"Acceptance probe failed: {name}")
    print(json.dumps(results, indent=2))
    first = results["cases"][0]
    if first["elapsed_seconds"] >= 30 or not first["response"]["coverage_complete"]:
        raise RuntimeError("Warm Cook Strait probe must finish completely within 30 seconds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://app:8004")
    asyncio.run(main(parser.parse_args().base_url))
