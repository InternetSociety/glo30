# Area-horizon performance handover 02

## Result and environment

Area-horizon v2 passes the three authenticated acceptance probes against the local Docker Compose service at `http://app:8004`.
The host exposes this service on port 8004.
The final run started at `2026-11-16T01:48:41.682062+00:00` (14:48:41 in New Zealand).

| Request | HTTP | Outcome | Complete coverage | Elapsed |
| --- | --- | --- | --- | ---: |
| Exact Cook Strait polygon, observer 575 m | 200 | `beyond_horizon` | true | 0.790 s |
| Same polygon, observer 12,000 m | 200 | `within_horizon` | false | 0.059 s |
| Point readiness probe at 174.2316077, -39.0035668 | 200 | `within_horizon` | true | 0.057 s |

The first cached-tile run after the service restart took 21.563 seconds for Cook Strait.
The repeated warm run took 0.790 seconds. Both meet the 30-second limit.
Elapsed times include HTTP handling, authentication, acquisition, hashing, raster work, and serialization.
The requests used the existing cache and real DEM files. The runner kept its temporary JWT in memory.

[Complete probe responses](area-horizon-performance-probes.json) include evidence, counts, source product prefixes, and all source SHA-256 hashes.
Run the probes again with:

```bash
docker compose run --rm app python -m scripts.benchmark_area_horizon
```

SHA-256 of the exact OpenAPI response bytes served during these probes:

```text
83267cd4de1de7ca39237ffdc6c49342a68f841b5fd6b2827cf3718148a6c27b
```

This records the running local service. No separate production deployment or reng code change forms part of this handover.
Source remains an uncommitted working-tree change based on `17cff060d61072c74f52c12d5d2c211b3cb54610`.
That Git revision identifies the baseline, not a commit containing v2. No message was sent to reng.

## Algorithm and contract

Polygon candidates are native cell centres inside the geometry plus samples on its exterior and hole boundaries.
Boundary samples include grid crossings and interval midpoints, with spacing no greater than one native cell.
Candidates use their containing cell's elevation and WGS84 geodesic distance.
A cell counts once even when it supplies several boundary samples or an interior centre too.

Polygon provenance reports `processor_version: area-horizon-v2`, `sampling: native_cell_centre`, and `tolerance_km: 0.05`.
Point/MultiPoint retains exact coordinates, an exact comparison, complete evidence scans, and `sampling: native_cell` with `tolerance_km: 0`.
All area-horizon responses now include `sea_level_assumed_tiles`.
Elevation response fields and viewshed behavior stay unchanged.

Each batch reads at most 64 by 64 cells once.
Wholly interior or exterior rectangles avoid per-cell geometry operations. Boundary-crossing batches use vectorized centre tests.
Before pruning, the processor checks availability and the maximum valid elevation across the complete batch.
Its conservative distance bound uses the nearest corner minus the larger batch diagonal, clamped to zero.
Tiles start with their nearest batch bound. Batches within each tile follow ascending bounds.
A batch skips candidate distances only when its best possible margin is strictly greater than the current best margin.
Such batches still contribute availability counts.

The Cook Strait completion log reported:

```text
elapsed=0.780s read_batches=3299 distance_batches=51 pruned_batches=3248
```

Thus, 3,248 of 3,299 raster batches skipped per-candidate distance work.
The response counted 12,539,122 valid candidates and zero unavailable candidates.
The processor no longer constructs or clips a polygon for every native cell, or runs per-cell golden-section searches.
The existing cell budget, deadline, cancellation checks, source hashing, and thread offload remain active.

Polygon processing returns after the first batch with a witness.
It returns the best candidate examined so far, partial counts, `coverage_complete: false`, and reason `stopped_at_witness`.
A usable observer cell inside or on the polygon gives an immediate zero-distance witness.
Unavailable observer terrain permits the normal search to continue. Observers inside holes receive no shortcut.

Complete beyond-horizon results retain the global minimum-margin evidence among v2 candidates, as agreed during planning.
For Cook Strait, that evidence is at `[174.10194444444443, -41.09027777777778]` with elevation 1199.640991 m.
Its distance is 348.012597 km and its horizon is 241.612668 km, giving a positive 106.399929 km margin.
The nearby boundary vertex is about 310.576 km away. It is not the strongest minimum-margin evidence.
This resolves the handover's conflicting expectation of evidence at approximately 311 km.
The high-observer response does use that nearby vertex as its early witness.

Only area horizon treats `catalogue_product_not_found` and `dem_object_not_found` geocells as logical 0 m terrain.
These geocells count as evaluated and appear in `sea_level_assumed_tiles`, without invented files or hashes.
Restricted tiles, published nodata, uncovered source areas, and upstream/network failures retain their prior handling.
Point elevations still return unavailable for unpublished geocells.

## Source sea-cell check

The exact polygon touches five geocells, not the full six-cell rectangular envelope mentioned in the handover.
`S41/E175` does not intersect it. All five actual sources appear in the complete response provenance.
Full bounded reads of every source confirmed these valid zero-elevation cells:

| Geocell | Valid zero cells | Masked cells | Nonfinite cells |
| --- | ---: | ---: | ---: |
| S41/E173 | 12,515,676 | 0 | 0 |
| S41/E174 | 12,854,488 | 0 | 0 |
| S42/E173 | 1,982,569 | 0 | 0 |
| S42/E174 | 9,968,059 | 0 | 0 |
| S42/E175 | 7,300,742 | 0 | 0 |

The sea portions therefore contain valid elevations, not masked or synthetic terrain.
The Cook Strait response needs no sea-level assumptions and reports an empty `sea_level_assumed_tiles` list.
[Source scan results](area-horizon-sea-cells.json) record the counts by full tile ID.

## Verification and changed tests

Checks ran through Docker Compose with CPython 3.14.10:

| Check | Result |
| --- | --- |
| Application image build | Passed |
| Full offline pytest suite with coverage | 148 passed, 1 live integration skipped |
| Coverage under existing configuration, including tests | 96% |
| Ruff lint | Passed |
| Ruff format check | Passed |
| mypy | Passed for 35 application source files |
| Git whitespace check | Passed |

Synthetic tests check majority-batch pruning, an isolated high cell in a later batch, and pruned versus exhaustive evidence.
They also check slivers, unpublished sea, restricted terrain, an interior tile with no original boundary, exact Point thresholds, and polygon tolerance.
The equality test checks the literal 0.05 km decision boundary and its next representable float.
Separate API cases check 49.9 m and 50.1 m, avoiding geodesic round-trip error in the equality fixture.
Existing tests continue to cover holes, disconnected components, antipodal geometry, provenance, upstream errors, and event-loop responsiveness.
A review checked pruning, native ownership, boundary sampling, and missing-data policy.
Rasterio still emits its existing affine-transform deprecation warning.

These existing tests changed for v2:

| Test or fixture | Change and reason |
| --- | --- |
| `terrain` fixture | Mock typed source acquisition so tests retain unavailable reasons. Missing fixture entries remain unknown coverage, not assumed sea. |
| `test_upstream_errors_are_not_missing_terrain` | Inject failures at typed acquisition, the common path now used by both endpoints. Error expectations are unchanged. |
| `test_horizon_deadline_covers_acquisition` | Delay typed acquisition instead of the previous wrapper. The expected HTTP 504 is unchanged. |
| `test_missing_polygon_cells_and_disconnected_witness` | Expect early-witness reason and zero unavailable cells before the unvisited component. |
| `test_horizon_worker_cooperatively_stops_on_deadline` | Instrument the batch worker that replaces cell clipping. Preserve HTTP 504 and worker-exit checks. |
| `test_polygon_reads_bounded_windows` | Check batch windows in the new worker. Use flat zero terrain to avoid an early witness during the full-scan assertion. |
| `test_swagger_request_examples_succeed_unchanged` | The area example now expects partial coverage and `stopped_at_witness`. Both published examples still return HTTP 200. |
| `test_native_restricted_and_absent_are_unavailable` | Also check preserved restricted/absent reasons while retaining the unchanged elevation-facing unavailable results. |

The original v1 handover remains historical evidence of that implementation and its performance limit.
Before reng Handover 07 repeats acceptance, its response schema must accept the new version, sampling values, provenance fields, and early-witness reason.
Existing v1 finder checkpoints remain evidence of their original runs. New runs must issue fresh requests.
