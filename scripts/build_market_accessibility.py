#!/usr/bin/env python3
"""Build Ethiopia market accessibility rasters, isochrones, and allocations."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import MergeAlg, Resampling
from rasterio.features import geometry_mask, rasterize, shapes
from rasterio.transform import Affine, rowcol
from rasterio.warp import reproject, transform_bounds
from shapely.geometry import shape
from skimage.graph import MCP_Geometric


DEFAULT_RESOLUTION_M = 500
DEFAULT_MARKETS = Path("data/market_centers/ethiopia_market_centers.geojson")
DEFAULT_ADMIN3 = Path("data/admin_boundaries/boundaries_3.geojson")
DEFAULT_WORLPOP = Path("data/worldpop/raw/2026/eth_pop_2026_CN_1km_R2025A_UA_v1.tif")
DEFAULT_ELEVATION = Path("data/elevation/Elevation/DEM_definedcoordvrt1.tif")
DEFAULT_LANDCOVER_DIR = Path("data/landcover_2025")
DEFAULT_ROADS_DIR = Path("data/ovt_roads")
DEFAULT_FRICTION_DIR = Path("data/travel_surface/friction")
DEFAULT_OUTPUT_DIR = Path("data/market_accessibility")

TARGET_CRS = (
    "+proj=aea +lat_1=5 +lat_2=13 +lat_0=0 +lon_0=39 "
    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
)
NODATA_FLOAT = -9999.0
NODATA_INT = 0
BARRIER_FRICTION = np.inf

ISOCHRONE_BANDS = [
    (0, 15, "0-15"),
    (15, 30, "15-30"),
    (30, 45, "30-45"),
    (45, 60, "45-60"),
    (60, 90, "60-90"),
    (90, 120, "90-120"),
    (120, 150, "120-150"),
    (150, 180, "150-180"),
    (180, math.inf, "180+"),
]

LANDCOVER_SPEEDS_KMH = {
    1: 0.0,  # Water: barrier unless crossed by roads.
    2: 2.5,  # Trees.
    4: 1.5,  # Flooded vegetation.
    5: 4.0,  # Crops.
    7: 5.0,  # Built area.
    8: 4.0,  # Bare ground.
    9: 0.0,  # Snow/ice: barrier unless crossed by roads.
    10: 4.5,  # Clouds: fallback to rangeland/default.
    11: 4.5,  # Rangeland.
}

ROAD_SPEEDS_LEAST_COST_KMH = {
    "motorway": 90.0,
    "trunk": 80.0,
    "primary": 70.0,
    "secondary": 60.0,
    "tertiary": 50.0,
    "unclassified": 30.0,
    "residential": 30.0,
    "living_street": 30.0,
    "service": 20.0,
    "track": 15.0,
    "path": 5.0,
    "footway": 5.0,
    "pedestrian": 5.0,
    "bridleway": 5.0,
    "cycleway": 5.0,
    "steps": 3.0,
}

ROAD_SPEEDS_WALK_KMH = {
    road_class: min(speed, 5.0)
    for road_class, speed in ROAD_SPEEDS_LEAST_COST_KMH.items()
}

ROAD_FILES = [
    "ovt_ethiopia_major_roads.geojson",
    "ovt_ethiopia_local_roads.geojson",
    "ovt_ethiopia_tracks_paths.geojson",
]


@dataclass(frozen=True)
class Grid:
    crs: str
    transform: Affine
    width: int
    height: int
    resolution_m: int
    mask: np.ndarray
    bounds: tuple[float, float, float, float]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution-m", type=int, default=DEFAULT_RESOLUTION_M)
    parser.add_argument("--markets", type=Path, default=DEFAULT_MARKETS)
    parser.add_argument("--admin3", type=Path, default=DEFAULT_ADMIN3)
    parser.add_argument("--worldpop", type=Path, default=DEFAULT_WORLPOP)
    parser.add_argument("--elevation", type=Path, default=DEFAULT_ELEVATION)
    parser.add_argument("--landcover-dir", type=Path, default=DEFAULT_LANDCOVER_DIR)
    parser.add_argument("--roads-dir", type=Path, default=DEFAULT_ROADS_DIR)
    parser.add_argument("--friction-dir", type=Path, default=DEFAULT_FRICTION_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--suffix", default=None, help="Optional output suffix. Default: {resolution}m.")
    parser.add_argument("--smoke-test", action="store_true", help="Run on an Addis Ababa subset and write under outputs/market_accessibility_smoke.")
    parser.add_argument("--skip-existing-friction", action="store_true", help="Reuse existing friction rasters if present.")
    return parser.parse_args()


def output_suffix(args: argparse.Namespace) -> str:
    return args.suffix or f"{args.resolution_m}m"


def load_boundary(admin3_path: Path, smoke_test: bool) -> gpd.GeoDataFrame:
    boundary = gpd.read_file(admin3_path).to_crs("EPSG:4326")
    if smoke_test:
        minx, miny, maxx, maxy = 38.55, 8.80, 39.10, 9.25
        bbox = gpd.GeoDataFrame(geometry=[shape({"type": "Polygon", "coordinates": [[
            (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)
        ]]})], crs="EPSG:4326")
        clipped = gpd.clip(boundary, bbox)
        if clipped.empty:
            raise RuntimeError("Smoke-test boundary is empty.")
        return clipped
    return boundary


def build_grid(boundary: gpd.GeoDataFrame, resolution_m: int) -> Grid:
    projected = boundary.to_crs(TARGET_CRS)
    geom = projected.geometry.union_all()
    minx, miny, maxx, maxy = geom.bounds
    minx = math.floor(minx / resolution_m) * resolution_m
    miny = math.floor(miny / resolution_m) * resolution_m
    maxx = math.ceil(maxx / resolution_m) * resolution_m
    maxy = math.ceil(maxy / resolution_m) * resolution_m
    width = int(round((maxx - minx) / resolution_m))
    height = int(round((maxy - miny) / resolution_m))
    transform = Affine(resolution_m, 0, minx, 0, -resolution_m, maxy)
    mask = geometry_mask(
        [geom],
        out_shape=(height, width),
        transform=transform,
        invert=True,
        all_touched=True,
    )
    return Grid(
        crs=TARGET_CRS,
        transform=transform,
        width=width,
        height=height,
        resolution_m=resolution_m,
        mask=mask,
        bounds=(minx, miny, maxx, maxy),
    )


def raster_profile(grid: Grid, dtype: str, nodata: float | int) -> dict[str, Any]:
    return {
        "driver": "GTiff",
        "height": grid.height,
        "width": grid.width,
        "count": 1,
        "dtype": dtype,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 3 if dtype.startswith("float") else 2,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "BIGTIFF": "IF_SAFER",
    }


def write_raster(path: Path, data: np.ndarray, grid: Grid, nodata: float | int = NODATA_FLOAT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = data.copy()
    if np.issubdtype(data.dtype, np.floating):
        data[~np.isfinite(data)] = nodata
    profile = raster_profile(grid, str(data.dtype), nodata)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def reproject_elevation(path: Path, grid: Grid) -> np.ndarray:
    dest = np.full((grid.height, grid.width), np.nan, dtype="float32")
    with rasterio.open(path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dest,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return dest


def slope_factor_from_elevation(elevation: np.ndarray, grid: Grid) -> np.ndarray:
    filled = elevation.copy()
    valid = np.isfinite(filled)
    if not np.any(valid):
        raise RuntimeError("No valid elevation values after reprojection.")
    filled[~valid] = float(np.nanmedian(filled[valid]))
    dzdy, dzdx = np.gradient(filled, grid.resolution_m, grid.resolution_m)
    slope = np.sqrt(dzdx**2 + dzdy**2).astype("float32")
    tobler_kmh = 6.0 * np.exp(-3.5 * np.abs(slope + 0.05))
    factor = np.clip(tobler_kmh / 5.0, 0.1, 1.0).astype("float32")
    factor[~grid.mask] = np.nan
    return factor


def reproject_landcover(landcover_dir: Path, grid: Grid) -> np.ndarray:
    dest = np.zeros((grid.height, grid.width), dtype="uint8")
    for path in sorted(landcover_dir.glob("*.tif")):
        with rasterio.open(path) as src:
            try:
                src_bounds_in_grid = transform_bounds(src.crs, grid.crs, *src.bounds, densify_pts=21)
            except Exception:
                continue
            if (
                src_bounds_in_grid[2] < grid.bounds[0]
                or src_bounds_in_grid[0] > grid.bounds[2]
                or src_bounds_in_grid[3] < grid.bounds[1]
                or src_bounds_in_grid[1] > grid.bounds[3]
            ):
                continue
            log(f"reproject landcover {path.name}")
            temp = np.zeros((grid.height, grid.width), dtype="uint8")
            reproject(
                source=rasterio.band(src, 1),
                destination=temp,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                dst_nodata=0,
                resampling=Resampling.mode,
            )
            dest[temp != 0] = temp[temp != 0]
    dest[~grid.mask] = 0
    return dest


def landcover_speed(landcover: np.ndarray, slope_factor: np.ndarray, grid: Grid) -> np.ndarray:
    speed = np.zeros(landcover.shape, dtype="float32")
    for code, kmh in LANDCOVER_SPEEDS_KMH.items():
        speed[landcover == code] = kmh
    speed[(landcover == 0) & grid.mask] = LANDCOVER_SPEEDS_KMH[11]
    speed *= np.nan_to_num(slope_factor, nan=1.0)
    speed[~grid.mask] = 0
    return speed


def road_speed_surface(roads_dir: Path, grid: Grid, speed_table: dict[str, float]) -> np.ndarray:
    road_speed = np.zeros((grid.height, grid.width), dtype="float32")
    for file_name in ROAD_FILES:
        path = roads_dir / file_name
        if not path.exists():
            raise FileNotFoundError(path)
        log(f"read roads {path.name}")
        source_crs = gpd.read_file(path, rows=1).crs
        bbox = transform_bounds(grid.crs, source_crs, *grid.bounds, densify_pts=21)
        roads = gpd.read_file(path, columns=["road_class", "geometry"], bbox=bbox).to_crs(grid.crs)
        roads = roads[roads.geometry.notna() & ~roads.geometry.is_empty]
        if roads.empty:
            continue
        for road_class, speed in sorted(speed_table.items(), key=lambda item: item[1]):
            subset = roads.loc[roads["road_class"] == road_class, "geometry"]
            if subset.empty:
                continue
            log(f"rasterize {len(subset):,} {road_class} roads at {speed:g} km/h")
            temp = np.zeros((grid.height, grid.width), dtype="uint8")
            rasterize(
                ((geom, 1) for geom in subset),
                out=temp,
                transform=grid.transform,
                fill=0,
                all_touched=True,
                merge_alg=MergeAlg.replace,
                dtype="uint8",
            )
            road_speed[temp > 0] = speed
    road_speed[~grid.mask] = 0
    return road_speed


def speed_to_friction(speed_kmh: np.ndarray, grid: Grid) -> np.ndarray:
    friction = np.full(speed_kmh.shape, BARRIER_FRICTION, dtype="float32")
    valid = grid.mask & (speed_kmh > 0)
    friction[valid] = 60.0 / (speed_kmh[valid] * 1000.0)
    return friction


def build_friction_surfaces(args: argparse.Namespace, grid: Grid, suffix: str) -> tuple[Path, Path, dict[str, Any]]:
    walk_path = args.friction_dir / f"ethiopia_walk_friction_{suffix}.tif"
    least_path = args.friction_dir / f"ethiopia_least_cost_friction_{suffix}.tif"
    metadata_path = args.friction_dir / f"ethiopia_market_friction_{suffix}_metadata.json"

    if args.skip_existing_friction and walk_path.exists() and least_path.exists():
        log("reuse existing friction rasters")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        return walk_path, least_path, metadata

    log("build elevation slope factor")
    elevation = reproject_elevation(args.elevation, grid)
    slope_factor = slope_factor_from_elevation(elevation, grid)

    log("build landcover speed surface")
    landcover = reproject_landcover(args.landcover_dir, grid)
    offroad_speed = landcover_speed(landcover, slope_factor, grid)

    log("build walking road speed surface")
    walk_roads = road_speed_surface(args.roads_dir, grid, ROAD_SPEEDS_WALK_KMH)
    walk_speed = np.maximum(offroad_speed, walk_roads * np.nan_to_num(slope_factor, nan=1.0))
    walk_speed[~grid.mask] = 0

    log("build least-cost road speed surface")
    least_roads = road_speed_surface(args.roads_dir, grid, ROAD_SPEEDS_LEAST_COST_KMH)
    least_speed = np.maximum(offroad_speed, least_roads)
    least_speed[~grid.mask] = 0

    log(f"write {walk_path}")
    write_raster(walk_path, speed_to_friction(walk_speed, grid), grid, NODATA_FLOAT)
    log(f"write {least_path}")
    write_raster(least_path, speed_to_friction(least_speed, grid), grid, NODATA_FLOAT)

    metadata = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "resolution_m": grid.resolution_m,
        "crs": grid.crs,
        "grid": {"width": grid.width, "height": grid.height, "bounds": list(grid.bounds)},
        "units": "minutes per meter",
        "formula": "friction_min_per_m = 60 / (speed_kmh * 1000)",
        "landcover_speeds_kmh": LANDCOVER_SPEEDS_KMH,
        "least_cost_road_speeds_kmh": ROAD_SPEEDS_LEAST_COST_KMH,
        "walking_road_speeds_kmh": ROAD_SPEEDS_WALK_KMH,
        "slope_adjustment": "Tobler-style factor: clamp((6*exp(-3.5*abs(slope+0.05))) / 5, 0.1, 1.0)",
        "sources": {
            "elevation": str(args.elevation),
            "landcover_dir": str(args.landcover_dir),
            "roads_dir": str(args.roads_dir),
        },
        "outputs": {"walking": str(walk_path), "least_cost": str(least_path)},
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"write {metadata_path}")
    return walk_path, least_path, metadata


def load_markets(path: Path, grid: Grid) -> tuple[gpd.GeoDataFrame, list[tuple[int, int]], np.ndarray, dict[int, list[int]]]:
    markets = gpd.read_file(path).to_crs(grid.crs)
    source_groups = np.zeros((grid.height, grid.width), dtype="int32")
    cell_to_group: dict[tuple[int, int], int] = {}
    group_members: dict[int, list[int]] = defaultdict(list)
    starts: list[tuple[int, int]] = []

    for market_index, geom in enumerate(markets.geometry):
        if geom is None or geom.is_empty:
            continue
        row, col = rowcol(grid.transform, geom.x, geom.y)
        if row < 0 or row >= grid.height or col < 0 or col >= grid.width or not grid.mask[row, col]:
            continue
        cell = (int(row), int(col))
        group_id = cell_to_group.get(cell)
        if group_id is None:
            group_id = len(cell_to_group) + 1
            cell_to_group[cell] = group_id
            source_groups[cell] = group_id
            starts.append(cell)
        group_members[group_id].append(market_index)

    if not starts:
        raise RuntimeError("No market points fell inside the analysis grid.")
    return markets, starts, source_groups, dict(group_members)


def read_friction(path: Path, grid: Grid) -> np.ndarray:
    with rasterio.open(path) as src:
        if src.width != grid.width or src.height != grid.height or src.transform != grid.transform:
            raise RuntimeError(f"Friction raster grid mismatch: {path}")
        data = src.read(1).astype("float32")
    data[~grid.mask] = np.inf
    data[~np.isfinite(data)] = np.inf
    data[data <= 0] = np.inf
    return data


def run_cost_distance(friction_path: Path, grid: Grid, starts: list[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray]:
    log(f"run cost-distance {friction_path.name} with {len(starts):,} source cells")
    friction = read_friction(friction_path, grid)
    for row, col in starts:
        friction[row, col] = min(friction[row, col], 0.000001)
    mcp = MCP_Geometric(friction, fully_connected=True, sampling=(grid.resolution_m, grid.resolution_m))
    costs, traceback = mcp.find_costs(starts)
    costs = costs.astype("float32")
    costs[~np.isfinite(costs)] = NODATA_FLOAT
    costs[~grid.mask] = NODATA_FLOAT
    costs[np.array([row for row, _ in starts]), np.array([col for _, col in starts])] = 0
    return costs, traceback


def allocation_from_traceback(traceback: np.ndarray, source_groups: np.ndarray, grid: Grid) -> np.ndarray:
    log("derive nearest-market allocation from traceback")
    flat_trace = traceback.ravel()
    offsets = np.array(MCP_Geometric(np.ones((1, 1), dtype="float32")).offsets)
    rows, cols = np.indices((grid.height, grid.width), dtype="int32")
    flat_index = np.arange(grid.height * grid.width, dtype="int64").reshape((grid.height, grid.width))
    pred = np.full(flat_index.shape, -1, dtype="int64")

    finite = grid.mask & (flat_trace.reshape(grid.height, grid.width) >= 0)
    source_mask = source_groups > 0
    pred[source_mask] = flat_index[source_mask]

    non_source = finite & ~source_mask
    trace_values = traceback[non_source]
    step_offsets = offsets[trace_values]
    pred_rows = rows[non_source] - step_offsets[:, 0]
    pred_cols = cols[non_source] - step_offsets[:, 1]
    ok = (pred_rows >= 0) & (pred_rows < grid.height) & (pred_cols >= 0) & (pred_cols < grid.width)
    pred_rows = pred_rows[ok]
    pred_cols = pred_cols[ok]
    non_source_rows = rows[non_source][ok]
    non_source_cols = cols[non_source][ok]
    pred[non_source_rows, non_source_cols] = flat_index[pred_rows, pred_cols]

    labels = source_groups.ravel().copy()
    pred_flat = pred.ravel()
    active = (pred_flat >= 0) & (labels == 0)
    iteration = 0
    while np.any(active):
        iteration += 1
        active_idx = np.flatnonzero(active)
        parent_idx = pred_flat[active_idx]
        parent_labels = labels[parent_idx]
        can_label = parent_labels > 0
        labels[active_idx[can_label]] = parent_labels[can_label]
        unresolved = active_idx[~can_label]
        if unresolved.size:
            pred_flat[unresolved] = pred_flat[parent_idx[~can_label]]
        active = (pred_flat >= 0) & (labels == 0)
        if iteration > 64:
            raise RuntimeError("Allocation traceback did not converge after 64 pointer-jumping iterations.")

    allocation = labels.reshape((grid.height, grid.width)).astype("int32")
    allocation[~grid.mask] = 0
    return allocation


def write_accessibility(path: Path, costs: np.ndarray, grid: Grid) -> None:
    data = costs.astype("float32")
    write_raster(path, data, grid, NODATA_FLOAT)
    log(f"write {path}")


def build_isochrones(costs: np.ndarray, mode: str, output_path: Path, grid: Grid) -> int:
    log(f"polygonize isochrones {mode}")
    band_array = np.zeros(costs.shape, dtype="uint8")
    valid = grid.mask & np.isfinite(costs) & (costs >= 0)
    for index, (lower, upper, _) in enumerate(ISOCHRONE_BANDS, start=1):
        if math.isinf(upper):
            mask = valid & (costs >= lower)
        else:
            mask = valid & (costs >= lower) & (costs < upper)
        band_array[mask] = index

    features: list[dict[str, Any]] = []
    band_lookup = {index: band for index, band in enumerate(ISOCHRONE_BANDS, start=1)}
    for geom, value in shapes(band_array, mask=band_array > 0, transform=grid.transform):
        value = int(value)
        lower, upper, label = band_lookup[value]
        features.append(
            {
                "mode": mode,
                "band": label,
                "min_minutes": lower,
                "max_minutes": -1 if math.isinf(upper) else upper,
                "geometry": shape(geom),
            }
        )

    gdf = gpd.GeoDataFrame(features, crs=grid.crs)
    dissolved = gdf.dissolve(by=["mode", "band", "min_minutes", "max_minutes"], as_index=False)
    dissolved.loc[dissolved["max_minutes"] < 0, "max_minutes"] = None
    dissolved = dissolved.to_crs("EPSG:4326")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dissolved.to_file(output_path, driver="GeoJSON")
    log(f"write {output_path} ({len(dissolved):,} features)")
    return len(dissolved)


def reproject_population(path: Path, grid: Grid) -> np.ndarray:
    log("reproject WorldPop population to analysis grid")
    dest = np.zeros((grid.height, grid.width), dtype="float32")
    with rasterio.open(path) as src:
        src_data = src.read(1).astype("float32")
        src_data[src_data == src.nodata] = 0
        src_data[src_data < 0] = 0
        reproject(
            source=src_data,
            destination=dest,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=0,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=0,
            resampling=Resampling.sum,
        )
    dest[~grid.mask] = 0
    dest[dest < 0] = 0
    return dest


def build_market_allocation(
    markets: gpd.GeoDataFrame,
    allocation: np.ndarray,
    group_members: dict[int, list[int]],
    population: np.ndarray,
    mode: str,
    output_path: Path,
    grid: Grid,
) -> dict[str, float]:
    log(f"aggregate allocated population {mode}")
    group_ids = allocation.ravel()
    pop_values = population.ravel().astype("float64")
    sums = np.bincount(group_ids, weights=pop_values, minlength=max(group_members) + 1)

    allocated = np.zeros(len(markets), dtype="float64")
    group_size = np.ones(len(markets), dtype="int32")
    for group_id, member_indexes in group_members.items():
        share = sums[group_id] / len(member_indexes)
        for market_index in member_indexes:
            allocated[market_index] = share
            group_size[market_index] = len(member_indexes)

    output = markets.to_crs("EPSG:4326").copy()
    output["mode"] = mode
    output["allocated_pop"] = allocated
    output["resolution_m"] = grid.resolution_m
    output["allocation_group_size"] = group_size
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_file(output_path, driver="GeoJSON")
    log(f"write {output_path} ({len(output):,} features)")
    return {
        "allocated_population_total": float(allocated.sum()),
        "grid_population_total": float(population.sum()),
        "market_count": int(len(output)),
        "source_cell_count": int(len(group_members)),
    }


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"write {path}")


def run(args: argparse.Namespace) -> None:
    if args.smoke_test:
        args.output_dir = Path("outputs/market_accessibility_smoke")
        args.friction_dir = Path("outputs/market_accessibility_smoke/friction")
        if args.suffix is None:
            args.suffix = f"smoke_{args.resolution_m}m"

    suffix = output_suffix(args)
    boundary = load_boundary(args.admin3, args.smoke_test)
    grid = build_grid(boundary, args.resolution_m)
    log(f"analysis grid {grid.width:,} x {grid.height:,}; mask cells {int(grid.mask.sum()):,}; resolution {grid.resolution_m} m")

    walk_friction, least_friction, friction_metadata = build_friction_surfaces(args, grid, suffix)
    markets, starts, source_groups, group_members = load_markets(args.markets, grid)
    if len(markets) != 2060 and not args.smoke_test:
        raise RuntimeError(f"Expected 2,060 markets, found {len(markets):,}.")
    log(f"market source cells {len(starts):,}; market features {len(markets):,}")
    population = reproject_population(args.worldpop, grid)

    metadata: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "resolution_m": grid.resolution_m,
        "crs": grid.crs,
        "grid": {"width": grid.width, "height": grid.height, "mask_cells": int(grid.mask.sum()), "bounds": list(grid.bounds)},
        "inputs": {
            "markets": str(args.markets),
            "admin3": str(args.admin3),
            "worldpop": str(args.worldpop),
        },
        "isochrone_bands": [
            {"label": label, "min_minutes": lower, "max_minutes": None if math.isinf(upper) else upper}
            for lower, upper, label in ISOCHRONE_BANDS
        ],
        "friction": friction_metadata,
        "outputs": {},
        "validation": {},
    }

    for mode, friction_path in [("walk", walk_friction), ("least_cost", least_friction)]:
        costs, traceback = run_cost_distance(friction_path, grid, starts)
        near_zero = [float(costs[row, col]) for row, col in starts[: min(100, len(starts))]]
        if max(near_zero, default=0) > 0.01:
            raise RuntimeError(f"Source market cells are not near zero for {mode}.")

        accessibility_path = args.output_dir / f"ethiopia_market_accessibility_{mode}_{suffix}.tif"
        write_accessibility(accessibility_path, costs, grid)
        allocation = allocation_from_traceback(traceback, source_groups, grid)
        allocation_path = args.output_dir / f"ethiopia_market_allocation_groups_{mode}_{suffix}.tif"
        write_raster(allocation_path, allocation, grid, NODATA_INT)
        log(f"write {allocation_path}")

        isochrone_path = args.output_dir / f"ethiopia_market_isochrones_{mode}_{suffix}.geojson"
        iso_count = build_isochrones(costs, mode, isochrone_path, grid)

        market_path = args.output_dir / f"ethiopia_market_centers_allocated_population_{mode}_{suffix}.geojson"
        allocation_stats = build_market_allocation(markets, allocation, group_members, population, mode, market_path, grid)

        metadata["outputs"][mode] = {
            "accessibility": str(accessibility_path),
            "allocation_groups": str(allocation_path),
            "isochrones": str(isochrone_path),
            "allocated_markets": str(market_path),
            "isochrone_feature_count": iso_count,
            **allocation_stats,
        }
        metadata["validation"][mode] = {
            "source_cells_checked": len(near_zero),
            "max_checked_source_cell_minutes": max(near_zero, default=0),
            "finite_accessibility_cells": int(np.sum(np.isfinite(costs) & (costs >= 0))),
        }

    metadata_path = args.output_dir / f"ethiopia_market_accessibility_{suffix}_metadata.json"
    write_metadata(metadata_path, metadata)


def main() -> int:
    run(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
