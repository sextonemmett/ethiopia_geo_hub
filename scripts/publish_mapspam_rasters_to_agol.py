#!/usr/bin/env python3
"""Publish Ethiopia MapSPAM crop production GeoTIFFs to AGOL hosted imagery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from arcgis.gis import GIS

from publish_geojson_to_agol import get_agol_browser_token, load_dotenv, validate_agol_user
from publish_worldpop_rasters_to_agol import (
    DEFAULT_PORTAL,
    ETHIOPIA_CORE_TEAM_GROUP_ID,
    delete_item,
    publish_spec,
)


DEFAULT_METADATA = Path("data/mapspam/mapspam_2020_v2_2_ethiopia_production_metadata.json")
DEFAULT_TAGS = ["ethiopia", "geo hub", "eth", "mapspam", "spam 2020 v2.2", "production", "total", "raster"]


def layer_specs(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    products = sorted(metadata["products"].values(), key=lambda item: item["rank"])
    release = metadata.get("release", "SPAM 2020 v2.2")
    year = metadata.get("year", 2020)
    source_doi = metadata.get("source_doi", "10.7910/DVN/SWPENT")
    specs: list[dict[str, Any]] = []
    for product in products:
        crop_name = product["crop_name"]
        crop_code = product["crop_code"]
        rank = product["rank"]
        title = f"Ethiopia MapSPAM {crop_name} Production {year}"
        description = (
            f"Ethiopia-clipped {release} gridded crop production raster for {crop_name}. "
            "Pixel values are total production for all technologies together (SPAM TA: rainfed plus irrigated). "
            f"This crop ranked {rank} among SPAM crops by summed total production inside Ethiopia. "
            f"Source DOI: https://doi.org/{source_doi}."
        )
        specs.append(
            {
                "key": product["key"],
                "title": title,
                "path": product["path"],
                "snippet": f"Ethiopia MapSPAM {crop_name} total production raster, {year}.",
                "description": description,
                "base_tags": DEFAULT_TAGS,
                "tags": [crop_name, crop_code, str(year), f"rank {rank}", "hosted imagery"],
            }
        )
    return specs


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
        "input": "local Ethiopia-clipped GeoTIFF uploaded to AGOL raster user store",
    }
    write_metadata(args.metadata, metadata)
    print(f"updated metadata {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
