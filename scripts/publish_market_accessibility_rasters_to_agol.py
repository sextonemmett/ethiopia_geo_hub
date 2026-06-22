#!/usr/bin/env python3
"""Publish only the continuous market accessibility rasters to AGOL."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
import warnings

from arcgis.gis import GIS
from arcgis.raster.analytics import copy_raster
import rasterio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_geojson_to_agol import agol_request, get_agol_browser_token, get_item, load_dotenv, validate_agol_user  # noqa: E402
from publish_worldpop_rasters_to_agol import (  # noqa: E402
    ETHIOPIA_CORE_TEAM_GROUP_ID,
    delete_item,
    service_name,
    share_item,
    update_item_metadata,
)


DEFAULT_METADATA = Path("data/market_accessibility/ethiopia_market_accessibility_500m_metadata.json")
DEFAULT_PORTAL = "https://www.arcgis.com"
DEFAULT_TAGS = ["ethiopia", "geo hub", "eth", "market access", "accessibility", "500m", "raster"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--group-id", action="append", default=[ETHIOPIA_CORE_TEAM_GROUP_ID])
    parser.add_argument("--delete-previous", action="store_true", help="Delete previously recorded accessibility image services after successful republish.")
    parser.add_argument("--service-name-suffix", default="_WGS84", help="Suffix for the AGOL Image Service name.")
    parser.add_argument("--only", default="all", help="Comma-separated raster keys to publish, or all.")
    parser.add_argument(
        "--source",
        choices=["original", "wgs84"],
        default="wgs84",
        help="Raster source to publish. Default: wgs84.",
    )
    return parser.parse_args()


def read_raster_profile(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as src:
        return {
            "crs": str(src.crs),
            "bounds": {
                "xmin": src.bounds.left,
                "ymin": src.bounds.bottom,
                "xmax": src.bounds.right,
                "ymax": src.bounds.top,
            },
            "width": src.width,
            "height": src.height,
            "pixel_size_x": abs(src.res[0]),
            "pixel_size_y": abs(src.res[1]),
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
        }


def raster_specs(metadata: dict[str, Any], source: str) -> list[dict[str, Any]]:
    outputs = metadata["outputs"]
    def raster_path(mode: str) -> str:
        if source == "original" and outputs[mode].get("accessibility_agol_stage"):
            return outputs[mode]["accessibility_agol_stage"]
        if source == "wgs84":
            return outputs[mode].get("accessibility_wgs84") or outputs[mode]["accessibility"]
        return outputs[mode]["accessibility"]

    return [
        {
            "key": "accessibility_walk",
            "mode": "walk",
            "title": "Ethiopia Market Accessibility Walking Travel Time 500m",
            "path": raster_path("walk"),
            "snippet": "Walking-only travel time in minutes to the nearest Ethiopia market center at 500 m resolution.",
            "description": (
                "Continuous raster of modeled walking-only travel time, in minutes, to the nearest market center "
                "in Ethiopia. This hosted imagery item is published as a continuous travel-time surface. "
                "Pixel values are minutes."
            ),
            "tags": ["walking", "travel time", "market centers", "continuous raster", "minutes", source],
        },
        {
            "key": "accessibility_least_cost",
            "mode": "least_cost",
            "title": "Ethiopia Market Accessibility Least-Cost Travel Time 500m",
            "path": raster_path("least_cost"),
            "snippet": "Least-cost travel time in minutes to the nearest Ethiopia market center at 500 m resolution.",
            "description": (
                "Continuous raster of modeled least-cost travel time, in minutes, to the nearest market center in Ethiopia. "
                "The least-cost mode uses motorized speeds on roads and walking speeds off-road. "
                "This hosted imagery item is published as a continuous travel-time surface. "
                "Pixel values are minutes."
            ),
            "tags": ["least cost", "travel time", "market centers", "continuous raster", "minutes", "roads", source],
        },
    ]


def item_summary(portal: str, token: str, item_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    item = get_item(portal, token, item_id)
    url = item.get("url")
    info = {}
    if url:
        info = agol_request("GET", url, context=f"ImageServer info failed for {item_id}", token=token, params={"f": "json"})
    return {
        "item_id": item_id,
        "title": item.get("title"),
        "type": item.get("type"),
        "url": url,
        "pixel_size_x": info.get("pixelSizeX"),
        "pixel_size_y": info.get("pixelSizeY"),
        "pixel_type": info.get("pixelType"),
        "source_profile": profile,
    }


def find_item_by_title(
    portal: str,
    token: str,
    username: str,
    title: str,
    item_type: str,
) -> dict[str, Any] | None:
    payload = agol_request(
        "GET",
        f"{portal}/sharing/rest/search",
        context=f"AGOL search failed for {title}",
        token=token,
        params={
            "f": "json",
            "q": f'title:"{title}" AND owner:{username} AND type:"{item_type}"',
            "num": 10,
        },
    )
    matches = [
        item
        for item in payload.get("results", [])
        if (item.get("title") or "").strip() == title and item.get("type") == item_type
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.get("modified") or 0, reverse=True)[0]


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
    profile = read_raster_profile(path)
    output_name = f"{service_name(spec['title'])}{service_name_suffix}"
    print(f"\n=== {spec['key']} ===", flush=True)
    print(f"  raster: {path}", flush=True)
    print(f"  output_name: {output_name}", flush=True)

    for search_title in (spec["title"], output_name):
        existing = find_item_by_title(portal, token, username, search_title, "Image Service")
        if existing:
            item_id = existing["id"]
            print(f"  Reusing existing imagery item with matching title {search_title!r}: {item_id}", flush=True)
            update_item_metadata(
                portal,
                token,
                username,
                item_id,
                {
                    "title": spec["title"],
                    "snippet": spec["snippet"],
                    "description": spec["description"],
                    "base_tags": DEFAULT_TAGS,
                    "tags": spec.get("tags") or [],
                },
            )
            share_item(portal, token, username, item_id, group_ids)
            return item_summary(portal, token, item_id, profile)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item = copy_raster(
            input_raster=str(path.resolve()),
            output_name=output_name,
            raster_type_name="Raster Dataset",
            gis=gis,
            future=False,
            tiles_only=False,
            context={"outSR": {"wkid": 4326}, "upload_properties": {"displayProgress": True}},
        )
    item_id = getattr(item, "itemid", None) or getattr(item, "id", None)
    if not item_id:
        raise RuntimeError(f"copy_raster returned no item id for {spec['key']}: {item!r}")

    update_item_metadata(
        portal,
        token,
        username,
        item_id,
        {
            "title": spec["title"],
            "snippet": spec["snippet"],
            "description": spec["description"],
            "base_tags": DEFAULT_TAGS,
            "tags": spec.get("tags") or [],
        },
    )
    share_item(portal, token, username, item_id, group_ids)
    summary = item_summary(portal, token, item_id, profile)
    print(f"  published item_id={item_id} url={summary.get('url')}", flush=True)
    return summary


def main() -> int:
    args = parse_args()
    load_dotenv()
    portal = os.getenv("AGOL_PORTAL_URL", DEFAULT_PORTAL).rstrip("/")
    client_id = os.getenv("AGOL_CLIENT_ID", "")
    redirect_uri = os.getenv("AGOL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    if not client_id:
        raise RuntimeError("Set AGOL_CLIENT_ID in .env or the environment.")

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    previous = dict(metadata.get("agol_hosted_imagery_items") or {})

    token = get_agol_browser_token(portal, client_id, redirect_uri)
    identity = validate_agol_user(portal, token)
    username = identity["username"]
    print(f"AGOL browser user validated: {username} ({identity.get('role')})", flush=True)
    gis = GIS(portal, token=token)

    specs = raster_specs(metadata, args.source)
    available = {spec["key"] for spec in specs}
    if args.only == "all":
        selected = available
    else:
        selected = {key.strip() for key in args.only.split(",") if key.strip()}
        unknown = selected - available
        if unknown:
            raise ValueError(f"Unknown --only key(s): {sorted(unknown)}. Available: {sorted(available)}")

    published = dict(previous)
    for spec in specs:
        if spec["key"] not in selected:
            continue
        published[spec["key"]] = publish_spec(gis, portal, token, username, spec, args.group_id, args.service_name_suffix)
        metadata["agol_hosted_imagery_items"] = published
        args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.delete_previous:
        for key, value in previous.items():
            item_id = value.get("item_id") if isinstance(value, dict) else None
            if item_id and item_id not in {item.get("item_id") for item in published.values()}:
                print(f"delete previous imagery item {key}: {item_id}", flush=True)
                delete_item(portal, token, username, item_id)

    metadata["agol_hosted_imagery_items"] = published
    metadata["agol_accessibility_raster_publish"] = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "EPSG:4326 GeoTIFF copies",
        "source_option": args.source,
        "service_name_suffix": args.service_name_suffix,
        "deleted_previous": bool(args.delete_previous),
    }
    args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nupdated metadata {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
