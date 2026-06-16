#!/usr/bin/env python3
"""Publish WorldPop GeoTIFFs to ArcGIS Online hosted imagery layers.

This uses the ArcGIS Python API imagery workflow:

1. Upload local GeoTIFFs to the AGOL raster user store.
2. Run Copy Raster with raster_type_name="Raster Dataset".
3. Verify the resulting ImageServer preserves raster extent, cell size, and
   float pixel type.

Do not use generic addItem(type="Image") for GeoTIFF publishing. That creates
plain image content items and can lose geospatial raster metadata.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import warnings

from arcgis.gis import GIS
from arcgis.raster.analytics import copy_raster
import rasterio

from publish_geojson_to_agol import (
    agol_request,
    get_agol_browser_token,
    get_item,
    load_dotenv,
    validate_agol_user,
)


DEFAULT_METADATA = Path("data/worldpop/worldpop_ethiopia_2026_metadata.json")
DEFAULT_PORTAL = "https://www.arcgis.com"
ETHIOPIA_CORE_TEAM_GROUP_ID = "31731315dc3a4f92adebf919c4651345"
DEFAULT_TAGS = ["ethiopia", "geo hub", "eth", "foundational_data", "worldpop", "population", "raster"]


def service_name(title: str) -> str:
    value = title.replace("&", "and")
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return value[:90]


def layer_specs(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    year = metadata["year"]
    release = metadata["release"]
    resolution = metadata["resolution"]
    products = metadata["products"]
    source_note = (
        f"WorldPop constrained Ethiopia population raster, {year}, "
        f"{resolution}, {release} {metadata.get('version', '')}. "
        "Values are people per grid cell."
    )
    return [
        {
            "key": "population_total",
            "title": f"Ethiopia WorldPop Population Total {resolution} {year}",
            "path": products["population_total"]["path"],
            "snippet": f"WorldPop constrained gridded total population for Ethiopia, {resolution}, {year}.",
            "description": source_note,
            "tags": ["total population", resolution, str(year)],
        },
        {
            "key": "pop_18_and_under",
            "title": f"Ethiopia WorldPop Population 18 and Under {resolution} {year}",
            "path": products["pop_18_and_under"]["path"],
            "snippet": f"Derived WorldPop gridded population age 18 and under for Ethiopia, {resolution}, {year}.",
            "description": (
                source_note
                + " Derived as ages 0-14 plus 80 percent of the 15-19 band, assuming uniform population within that band."
            ),
            "tags": ["age structure", "18 and under", resolution, str(year)],
        },
        {
            "key": "pop_15_64",
            "title": f"Ethiopia WorldPop Population 15-64 {resolution} {year}",
            "path": products["pop_15_64"]["path"],
            "snippet": f"Derived WorldPop gridded population ages 15-64 for Ethiopia, {resolution}, {year}.",
            "description": source_note + " Derived as the sum of WorldPop total-sex age bands 15-19 through 60-64.",
            "tags": ["age structure", "15-64", resolution, str(year)],
        },
    ]


def merged_tags(spec: dict[str, Any]) -> str:
    tags: list[str] = []
    for tag in [*DEFAULT_TAGS, *(spec.get("tags") or [])]:
        tag = str(tag).strip().lower()
        if tag and tag not in tags:
            tags.append(tag)
    return ",".join(tags)


def read_raster_profile(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        return {
            "crs": str(dataset.crs),
            "bounds": {
                "xmin": dataset.bounds.left,
                "ymin": dataset.bounds.bottom,
                "xmax": dataset.bounds.right,
                "ymax": dataset.bounds.top,
            },
            "width": dataset.width,
            "height": dataset.height,
            "pixel_size_x": abs(dataset.res[0]),
            "pixel_size_y": abs(dataset.res[1]),
            "dtype": dataset.dtypes[0],
            "nodata": dataset.nodata,
        }


def close_enough(left: float, right: float, tolerance: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0, abs_tol=tolerance)


def validate_image_service(
    portal: str,
    token: str,
    item_id: str,
    expected: dict[str, Any],
    *,
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    item = get_item(portal, token, item_id)
    if item.get("type") != "Image Service":
        raise RuntimeError(f"{item_id} is type {item.get('type')!r}, expected Image Service.")
    url = item.get("url")
    if not url:
        raise RuntimeError(f"{item_id} has no ImageServer URL.")

    info = agol_request("GET", url, context=f"ImageServer info failed for {item_id}", token=token, params={"f": "json"})
    extent = info.get("fullExtent") or info.get("extent") or {}
    failures = []
    for service_key, expected_key in [("xmin", "xmin"), ("ymin", "ymin"), ("xmax", "xmax"), ("ymax", "ymax")]:
        if not close_enough(extent.get(service_key), expected["bounds"][expected_key], tolerance):
            failures.append(f"{service_key}={extent.get(service_key)} expected {expected['bounds'][expected_key]}")
    if not close_enough(info.get("pixelSizeX"), expected["pixel_size_x"], tolerance):
        failures.append(f"pixelSizeX={info.get('pixelSizeX')} expected {expected['pixel_size_x']}")
    if not close_enough(info.get("pixelSizeY"), expected["pixel_size_y"], tolerance):
        failures.append(f"pixelSizeY={info.get('pixelSizeY')} expected {expected['pixel_size_y']}")
    if info.get("pixelType") not in {"F32", "F64"}:
        failures.append(f"pixelType={info.get('pixelType')} expected float")
    if failures:
        raise RuntimeError(f"Image service validation failed for {item_id}: " + "; ".join(failures))
    return {"item": item, "service_info": info}


def update_item_metadata(portal: str, token: str, username: str, item_id: str, spec: dict[str, Any]) -> None:
    result = agol_request(
        "POST",
        f"{portal}/sharing/rest/content/users/{username}/items/{item_id}/update",
        context=f"Could not update metadata for {item_id}",
        token=token,
        data={
            "f": "json",
            "title": spec["title"],
            "snippet": spec["snippet"],
            "description": spec["description"],
            "tags": merged_tags(spec),
            "licenseInfo": "Creative Commons Attribution 4.0 International",
        },
    )
    if not result.get("success"):
        raise RuntimeError(f"Metadata update did not succeed for {item_id}: {result}")


def share_item(portal: str, token: str, username: str, item_id: str, group_ids: list[str]) -> None:
    if not group_ids:
        return
    result = agol_request(
        "POST",
        f"{portal}/sharing/rest/content/users/{username}/items/{item_id}/share",
        context=f"Could not share {item_id}",
        token=token,
        data={
            "f": "json",
            "everyone": "false",
            "org": "false",
            "groups": ",".join(group_ids),
            "confirmItemControl": "true",
        },
    )
    not_shared = result.get("notSharedWith") or []
    if not_shared:
        raise RuntimeError(f"{item_id} was not shared with these groups: {not_shared}")


def delete_item(portal: str, token: str, username: str, item_id: str) -> None:
    result = agol_request(
        "POST",
        f"{portal}/sharing/rest/content/users/{username}/items/{item_id}/delete",
        context=f"Could not delete {item_id}",
        token=token,
        data={"f": "json"},
    )
    if not result.get("success"):
        raise RuntimeError(f"Delete did not succeed for {item_id}: {result}")


def publish_spec(
    gis: GIS,
    portal: str,
    token: str,
    username: str,
    spec: dict[str, Any],
    group_ids: list[str],
    service_name_suffix: str,
) -> dict[str, Any]:
    path = Path(spec["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    expected = read_raster_profile(path)
    output_name = f"{service_name(spec['title'])}{service_name_suffix}"
    print(f"publish {spec['key']}: {path} -> {output_name}", flush=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item = copy_raster(
            input_raster=str(path.resolve()),
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
        raise RuntimeError(f"copy_raster returned no item id for {spec['key']}: {item!r}")

    update_item_metadata(portal, token, username, item_id, spec)
    share_item(portal, token, username, item_id, group_ids)
    validated = validate_image_service(portal, token, item_id, expected)
    item_info = validated["item"]
    info = validated["service_info"]
    print(
        f"validated {spec['key']}: item_id={item_id}, "
        f"pixel={info.get('pixelSizeX')}/{info.get('pixelSizeY')}, pixelType={info.get('pixelType')}",
        flush=True,
    )
    return {
        "item_id": item_id,
        "url": item_info.get("url"),
        "type": item_info.get("type"),
        "pixel_size_x": info.get("pixelSizeX"),
        "pixel_size_y": info.get("pixelSizeY"),
        "pixel_type": info.get("pixelType"),
    }


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--group-id", action="append", default=[ETHIOPIA_CORE_TEAM_GROUP_ID])
    parser.add_argument("--delete-existing", action="store_true", help="Delete current hosted imagery items before republishing.")
    parser.add_argument("--delete-item-id", action="append", default=[], help="Additional obsolete AGOL item ID to delete.")
    parser.add_argument(
        "--service-name-suffix",
        default="",
        help="Suffix for the ImageServer service name when AGOL still reserves a deleted service name.",
    )
    return parser.parse_args()


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

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    existing = metadata.get("agol_hosted_imagery_items") or {}
    delete_ids = list(args.delete_item_id)
    if args.delete_existing:
        for value in existing.values():
            if isinstance(value, dict) and value.get("item_id"):
                delete_ids.append(value["item_id"])
            elif isinstance(value, str):
                delete_ids.append(value)
    for item_id in dict.fromkeys(delete_ids):
        print(f"delete obsolete item {item_id}", flush=True)
        delete_item(portal, token, username, item_id)

    published: dict[str, dict[str, Any]] = {}
    for spec in layer_specs(metadata):
        published[spec["key"]] = publish_spec(
            gis,
            portal,
            token,
            username,
            spec,
            args.group_id,
            args.service_name_suffix,
        )

    metadata["agol_hosted_imagery_items"] = published
    metadata["agol_publish_method"] = {
        "tool": "arcgis.raster.analytics.copy_raster",
        "raster_type_name": "Raster Dataset",
        "tiles_only": False,
        "input": "local GeoTIFF uploaded to AGOL raster user store",
    }
    write_metadata(args.metadata, metadata)
    print(f"updated metadata {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
