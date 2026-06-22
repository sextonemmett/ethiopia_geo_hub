#!/usr/bin/env python3
"""Publish Fathom flood GeoTIFF tiles to AGOL as one hosted imagery layer."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any
import warnings

from arcgis.gis import GIS
from arcgis.raster.analytics import copy_raster
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import bounds as window_bounds
from rasterio.windows import from_bounds
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_geojson_to_agol import (  # noqa: E402
    agol_request,
    get_agol_browser_token,
    get_item,
    load_dotenv,
    validate_agol_user,
)
from publish_worldpop_rasters_to_agol import (  # noqa: E402
    DEFAULT_PORTAL,
    ETHIOPIA_CORE_TEAM_GROUP_ID,
    delete_item,
    service_name,
    share_item,
    update_item_metadata,
)


DEFAULT_METADATA = Path("data/floods/fathom_fluvial_undefended_1in100_metadata.json")
DEFAULT_TILE_DIR = Path("data/floods/1in100")
DEFAULT_MOSAIC = Path("data/floods/products/fathom_fluvial_undefended_1in100_2030_ssp2_45_ethiopia_mosaic.tif")
DEFAULT_MASK = Path("data/floods/products/fathom_fluvial_undefended_1in100_2030_ssp2_45_ethiopia_binary_10cm.tif")
DEFAULT_BOUNDARY = Path("data/admin_boundaries/boundaries_1.geojson")
SOURCE_NODATA = -32767
MASK_NODATA = 255
DEFAULT_THRESHOLD_CM = 10
EXPECTED_PIXEL_TYPES = {"U8", "S16", "F32", "F64"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--tile-dir", type=Path, default=DEFAULT_TILE_DIR)
    parser.add_argument("--mosaic-path", type=Path, default=DEFAULT_MOSAIC)
    parser.add_argument("--mask-path", type=Path, default=DEFAULT_MASK)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--threshold-cm", type=int, default=DEFAULT_THRESHOLD_CM)
    parser.add_argument("--rebuild-mosaic", action="store_true")
    parser.add_argument("--rebuild-mask", action="store_true")
    parser.add_argument("--input-mode", choices=["mosaic", "tiles"], default="mosaic")
    parser.add_argument("--group-id", action="append", default=[ETHIOPIA_CORE_TEAM_GROUP_ID])
    parser.add_argument("--delete-existing", action="store_true", help="Delete the metadata item_id before republishing.")
    parser.add_argument("--delete-item-id", action="append", default=[], help="Additional obsolete AGOL item ID to delete.")
    parser.add_argument(
        "--service-name-suffix",
        default="",
        help="Suffix for the ImageServer service name when AGOL still reserves a deleted service name.",
    )
    parser.add_argument(
        "--keep-staged",
        action="store_true",
        help="Keep temporary upload copies for inspection instead of deleting them after publishing.",
    )
    return parser.parse_args()


def load_metadata(path: Path, tile_dir: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "dataset": "Fathom Fluvial Undefended 1-in-100-year Flood Zones",
        "return_period": "1-in-100 year",
        "hazard": "fluvial",
        "defense": "undefended",
        "scenario_year": 2030,
        "scenario": "SSP2-4.5",
        "source_tile_dir": str(tile_dir),
        "source_nodata": SOURCE_NODATA,
        "units": "centimeters",
    }


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tile_paths(tile_dir: Path) -> list[Path]:
    paths = sorted(tile_dir.glob("*.tif"))
    if not paths:
        raise FileNotFoundError(f"No GeoTIFF tiles found in {tile_dir}")
    return paths


def source_details(paths: list[Path]) -> dict[str, Any]:
    names = "\n".join(path.name for path in paths)
    years = sorted(set(re.findall(r"(20\d{2})", names)))
    scenarios = sorted(set(re.findall(r"(SSP\d+_\d+(?:\.\d+)?)", names, flags=re.IGNORECASE)))
    scenario = scenarios[0].upper().replace("_", "-") if len(scenarios) == 1 else None
    return {
        "scenario_year": int(years[0]) if len(years) == 1 else None,
        "scenario": scenario,
    }


def source_profile(paths: list[Path]) -> dict[str, Any]:
    left = bottom = math.inf
    right = top = -math.inf
    crs = None
    dtype = None
    res_x = res_y = None
    total_pixels = 0
    for path in paths:
        with rasterio.open(path) as dataset:
            if crs is None:
                crs = dataset.crs
                dtype = dataset.dtypes[0]
                res_x = abs(dataset.res[0])
                res_y = abs(dataset.res[1])
            elif dataset.crs != crs or dataset.dtypes[0] != dtype:
                raise RuntimeError(f"{path} does not match the first tile CRS/dtype.")
            if not math.isclose(abs(dataset.res[0]), res_x or 0, rel_tol=0, abs_tol=1e-12):
                raise RuntimeError(f"{path} has unexpected x resolution {dataset.res[0]}.")
            if not math.isclose(abs(dataset.res[1]), res_y or 0, rel_tol=0, abs_tol=1e-12):
                raise RuntimeError(f"{path} has unexpected y resolution {dataset.res[1]}.")
            bounds = dataset.bounds
            left = min(left, bounds.left)
            bottom = min(bottom, bounds.bottom)
            right = max(right, bounds.right)
            top = max(top, bounds.top)
            total_pixels += dataset.width * dataset.height
    width = round((right - left) / (res_x or 1))
    height = round((top - bottom) / (res_y or 1))
    return {
        "crs": str(crs),
        "dtype": dtype,
        "bounds": {"xmin": left, "ymin": bottom, "xmax": right, "ymax": top},
        "pixel_size_x": res_x,
        "pixel_size_y": res_y,
        "width": width,
        "height": height,
        "tile_count": len(paths),
        "total_source_pixels": total_pixels,
        "source_nodata": SOURCE_NODATA,
    }


def build_mosaic(paths: list[Path], output_path: Path, profile: dict[str, Any], rebuild: bool) -> Path:
    if output_path.exists() and not rebuild:
        print(f"use existing mosaic {output_path}", flush=True)
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bounds = profile["bounds"]
    transform = rasterio.Affine(
        profile["pixel_size_x"],
        0,
        bounds["xmin"],
        0,
        -profile["pixel_size_y"],
        bounds["ymax"],
    )
    output_profile = {
        "driver": "GTiff",
        "height": profile["height"],
        "width": profile["width"],
        "count": 1,
        "dtype": profile["dtype"],
        "crs": profile["crs"],
        "transform": transform,
        "nodata": SOURCE_NODATA,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "DEFLATE",
        "predictor": 2,
        "zlevel": 6,
        "BIGTIFF": "YES",
    }

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    print(
        f"build mosaic {output_path}: {profile['width']}x{profile['height']} from {len(paths)} tiles",
        flush=True,
    )
    block_size = 512
    with rasterio.open(temp_path, "w", **output_profile) as output:
        nodata_block = np.full((block_size, block_size), SOURCE_NODATA, dtype=profile["dtype"])
        for row in range(0, profile["height"], block_size):
            rows = min(block_size, profile["height"] - row)
            for col in range(0, profile["width"], block_size):
                cols = min(block_size, profile["width"] - col)
                output.write(nodata_block[:rows, :cols], 1, window=Window(col, row, cols, rows))

        for index, path in enumerate(paths, start=1):
            with rasterio.open(path) as source:
                window = Window(
                    round((source.bounds.left - bounds["xmin"]) / profile["pixel_size_x"]),
                    round((bounds["ymax"] - source.bounds.top) / profile["pixel_size_y"]),
                    source.width,
                    source.height,
                )
                output.write(source.read(1), 1, window=window)
            if index == 1 or index % 20 == 0 or index == len(paths):
                print(f"  wrote {index}/{len(paths)} tiles", flush=True)
    temp_path.replace(output_path)
    return output_path


def boundary_geometry(path: Path, crs: str) -> Any:
    boundaries = gpd.read_file(path)
    if boundaries.empty:
        raise RuntimeError(f"No boundary features found in {path}")
    boundaries = boundaries.to_crs(crs)
    return boundaries.union_all()


def build_binary_mask(
    source_path: Path,
    boundary_path: Path,
    output_path: Path,
    threshold_cm: int,
    rebuild: bool,
) -> tuple[Path, dict[str, Any]]:
    if output_path.exists() and not rebuild:
        print(f"use existing binary mask {output_path}", flush=True)
        with rasterio.open(output_path) as dataset:
            return output_path, {
                "crs": str(dataset.crs),
                "bounds": {
                    "xmin": dataset.bounds.left,
                    "ymin": dataset.bounds.bottom,
                    "xmax": dataset.bounds.right,
                    "ymax": dataset.bounds.top,
                },
                "pixel_size_x": abs(dataset.res[0]),
                "pixel_size_y": abs(dataset.res[1]),
                "width": dataset.width,
                "height": dataset.height,
                "dtype": dataset.dtypes[0],
                "nodata": dataset.nodata,
            }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(source_path) as source:
        geometry = boundary_geometry(boundary_path, str(source.crs))
        crop = from_bounds(*geometry.bounds, transform=source.transform).round_offsets().round_lengths()
        crop = crop.intersection(Window(0, 0, source.width, source.height))
        crop_bounds = window_bounds(crop, source.transform)
        transform = rasterio.transform.from_origin(
            crop_bounds[0],
            crop_bounds[3],
            abs(source.res[0]),
            abs(source.res[1]),
        )
        output_profile = source.profile.copy()
        output_profile.update(
            {
                "height": int(crop.height),
                "width": int(crop.width),
                "count": 1,
                "dtype": "uint8",
                "transform": transform,
                "nodata": MASK_NODATA,
                "compress": "DEFLATE",
                "predictor": 1,
                "zlevel": 6,
                "BIGTIFF": "IF_SAFER",
            }
        )

        temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        if temp_path.exists():
            temp_path.unlink()
        print(
            f"build binary mask {output_path}: {int(crop.width)}x{int(crop.height)}, "
            f"threshold >= {threshold_cm} cm",
            flush=True,
        )
        source_transform = source.window_transform(crop)
        with rasterio.open(temp_path, "w", **output_profile) as output:
            for _, output_window in output.block_windows(1):
                source_window = Window(
                    crop.col_off + output_window.col_off,
                    crop.row_off + output_window.row_off,
                    output_window.width,
                    output_window.height,
                )
                values = source.read(1, window=source_window)
                block_transform = rasterio.windows.transform(output_window, source_transform)
                inside = geometry_mask(
                    [geometry],
                    out_shape=values.shape,
                    transform=block_transform,
                    invert=True,
                    all_touched=False,
                )
                valid = (values != SOURCE_NODATA) & inside
                mask = np.full(values.shape, MASK_NODATA, dtype=np.uint8)
                mask[valid] = (values[valid] >= threshold_cm).astype(np.uint8)
                output.write(mask, 1, window=output_window)
        temp_path.replace(output_path)

    with rasterio.open(output_path) as dataset:
        profile = {
            "crs": str(dataset.crs),
            "bounds": {
                "xmin": dataset.bounds.left,
                "ymin": dataset.bounds.bottom,
                "xmax": dataset.bounds.right,
                "ymax": dataset.bounds.top,
            },
            "pixel_size_x": abs(dataset.res[0]),
            "pixel_size_y": abs(dataset.res[1]),
            "width": dataset.width,
            "height": dataset.height,
            "dtype": dataset.dtypes[0],
            "nodata": dataset.nodata,
        }
    return output_path, profile


def stage_tiles_with_nodata(paths: list[Path], keep_staged: bool) -> tuple[list[Path], tempfile.TemporaryDirectory[str] | None]:
    if keep_staged:
        staged_dir = Path(tempfile.mkdtemp(prefix="fathom_flood_agol_"))
        temp_dir = None
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="fathom_flood_agol_")
        staged_dir = Path(temp_dir.name)
    staged_paths = []
    for path in paths:
        staged = staged_dir / path.name
        shutil.copy2(path, staged)
        with rasterio.open(staged, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dataset:
            dataset.nodata = SOURCE_NODATA
        staged_paths.append(staged)
    if keep_staged:
        print(f"kept staged upload tiles at {staged_dir}", flush=True)
        return staged_paths, temp_dir
    return staged_paths, temp_dir


def close_enough(left: float | None, right: float, tolerance: float) -> bool:
    if left is None:
        return False
    return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)


def validate_image_service(portal: str, token: str, item_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    item = get_item(portal, token, item_id)
    if item.get("type") != "Image Service":
        raise RuntimeError(f"{item_id} is type {item.get('type')!r}, expected Image Service.")
    url = item.get("url")
    if not url:
        raise RuntimeError(f"{item_id} has no ImageServer URL.")

    info = agol_request("GET", url, context=f"ImageServer info failed for {item_id}", token=token, params={"f": "json"})
    extent = info.get("fullExtent") or info.get("extent") or {}
    tolerance = max(
        1e-6,
        (expected["pixel_size_x"] or 0) / 2,
        (expected["pixel_size_y"] or 0) / 2,
    )
    failures = []
    for key in ("xmin", "ymin", "xmax", "ymax"):
        if not close_enough(extent.get(key), expected["bounds"][key], tolerance):
            failures.append(f"{key}={extent.get(key)} expected {expected['bounds'][key]}")
    if not close_enough(info.get("pixelSizeX"), expected["pixel_size_x"], 1e-8):
        failures.append(f"pixelSizeX={info.get('pixelSizeX')} expected {expected['pixel_size_x']}")
    if not close_enough(info.get("pixelSizeY"), expected["pixel_size_y"], 1e-8):
        failures.append(f"pixelSizeY={info.get('pixelSizeY')} expected {expected['pixel_size_y']}")
    if info.get("pixelType") not in EXPECTED_PIXEL_TYPES:
        failures.append(f"pixelType={info.get('pixelType')} expected one of {sorted(EXPECTED_PIXEL_TYPES)}")
    if failures:
        raise RuntimeError(f"Image service validation failed for {item_id}: " + "; ".join(failures))
    return {"item": item, "service_info": info}


def main() -> int:
    args = parse_args()
    load_dotenv()
    portal = os.getenv("AGOL_PORTAL_URL", DEFAULT_PORTAL).rstrip("/")
    token = get_agol_browser_token(
        portal,
        os.getenv("AGOL_CLIENT_ID", ""),
        os.getenv("AGOL_REDIRECT_URI", "http://127.0.0.1:8765/callback"),
    )
    identity = validate_agol_user(portal, token)
    username = identity["username"]
    print(f"AGOL browser user validated: {username} ({identity.get('role')})", flush=True)
    gis = GIS(portal, token=token)

    metadata = load_metadata(args.metadata, args.tile_dir)
    paths = tile_paths(args.tile_dir)
    expected = source_profile(paths)
    print(
        f"found {expected['tile_count']} flood tiles, pixel={expected['pixel_size_x']}/{expected['pixel_size_y']}, "
        f"dtype={expected['dtype']}",
        flush=True,
    )

    delete_ids = list(args.delete_item_id)
    existing_item = metadata.get("agol_hosted_imagery_item", {}).get("item_id")
    if args.delete_existing and existing_item:
        delete_ids.append(existing_item)
    for item_id in dict.fromkeys(delete_ids):
        print(f"delete obsolete item {item_id}", flush=True)
        delete_item(portal, token, username, item_id)

    details = source_details(paths)
    scenario_year = details.get("scenario_year") or metadata.get("scenario_year") or 2030
    scenario = details.get("scenario") or metadata.get("scenario") or "SSP2-4.5"
    threshold_label = f"{args.threshold_cm}cm"
    title = (
        f"Ethiopia Fathom Fluvial Undefended Flood Mask {threshold_label} "
        f"1-in-100 Year {scenario_year} {scenario}"
    )
    spec = {
        "key": "fathom_fluvial_undefended_1in100",
        "title": title,
        "snippet": (
            f"Binary mask of Fathom fluvial undefended {scenario_year} {scenario} "
            f"1-in-100-year flood depth >={args.threshold_cm} cm for Ethiopia."
        ),
        "description": (
            "Hosted imagery layer built from local Fathom fluvial undefended 1-in-100-year flood GeoTIFF tiles. "
            "The published raster is clipped to the Ethiopia admin-1 boundary dissolved from "
            "data/admin_boundaries/boundaries_1.geojson and encoded as a binary mask: 1 means source flood depth "
            f"is at least {args.threshold_cm} cm, 0 means below {args.threshold_cm} cm inside Ethiopia, "
            "and 255 is no-data outside Ethiopia."
        ),
        "base_tags": ["ethiopia", "geo hub", "eth", "foundational_data", "raster"],
        "tags": [
            "fathom",
            "flood",
            "fluvial",
            "undefended",
            "1-in-100",
            "hazard",
            str(scenario_year),
            scenario.lower(),
            "binary mask",
            threshold_label,
        ],
    }
    output_name = f"{service_name(title)}{args.service_name_suffix}"

    with ExitStack() as stack:
        if args.input_mode == "tiles":
            staged_paths, temp_dir = stage_tiles_with_nodata(paths, args.keep_staged)
            if temp_dir is not None:
                stack.callback(temp_dir.cleanup)
            input_raster: str | list[str] = [str(path.resolve()) for path in staged_paths]
            input_description = "list of local GeoTIFF tiles staged with a no-data tag"
            print(f"publish {spec['key']}: {len(staged_paths)} tiles -> {output_name}", flush=True)
        else:
            mosaic_path = build_mosaic(paths, args.mosaic_path, expected, args.rebuild_mosaic)
            mask_path, expected = build_binary_mask(
                mosaic_path,
                args.boundary,
                args.mask_path,
                args.threshold_cm,
                args.rebuild_mask,
            )
            input_raster = str(mask_path.resolve())
            input_description = "single local binary mask GeoTIFF clipped to Ethiopia"
            print(f"publish {spec['key']}: {mask_path} -> {output_name}", flush=True)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            item = copy_raster(
                input_raster=input_raster,
                output_name=output_name,
                raster_type_name="Raster Dataset",
                gis=gis,
                future=False,
                tiles_only=False,
                context={
                    "outSR": {"wkid": 4326},
                    "upload_properties": {"displayProgress": True},
                },
            )

    item_id = getattr(item, "itemid", None) or getattr(item, "id", None)
    if not item_id:
        raise RuntimeError(f"copy_raster returned no item id: {item!r}")
    update_item_metadata(portal, token, username, item_id, spec)
    share_item(portal, token, username, item_id, args.group_id)
    validated = validate_image_service(portal, token, item_id, expected)
    item_info = validated["item"]
    info = validated["service_info"]
    print(
        f"validated flood layer: item_id={item_id}, pixel={info.get('pixelSizeX')}/{info.get('pixelSizeY')}, "
        f"pixelType={info.get('pixelType')}",
        flush=True,
    )

    metadata.update(
        {
            "source_tile_dir": str(args.tile_dir),
            "units": "centimeters",
            "scenario_year": scenario_year,
            "scenario": scenario,
            "derived_profile": expected,
            "source_profile": source_profile(paths),
            "processing": {
                "boundary": str(args.boundary),
                "clip": "Ethiopia boundary dissolved from admin-1 polygons",
                "threshold_cm": args.threshold_cm,
                "binary_values": {"0": f"inside Ethiopia and < {args.threshold_cm} cm", "1": f">= {args.threshold_cm} cm", "255": "no-data"},
            },
            "agol_hosted_imagery_item": {
                "item_id": item_id,
                "url": item_info.get("url"),
                "type": item_info.get("type"),
                "pixel_size_x": info.get("pixelSizeX"),
                "pixel_size_y": info.get("pixelSizeY"),
                "pixel_type": info.get("pixelType"),
            },
            "agol_publish_method": {
                "tool": "arcgis.raster.analytics.copy_raster",
                "raster_type_name": "Raster Dataset",
                "tiles_only": False,
                "input": input_description,
                "source_nodata": SOURCE_NODATA,
            },
        }
    )
    write_metadata(args.metadata, metadata)
    print(f"updated metadata {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
