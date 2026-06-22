#!/usr/bin/env python3
"""Publish Ethiopia nighttime lights GeoTIFF to AGOL as an imagery layer."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from arcgis.gis import GIS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_worldpop_rasters_to_agol import (  # noqa: E402
    DEFAULT_PORTAL,
    ETHIOPIA_CORE_TEAM_GROUP_ID,
    get_agol_browser_token,
    load_dotenv,
    publish_spec,
    validate_agol_user,
)


DEFAULT_METADATA = Path("data/nighttime_lights/ethiopia_blackmarble_2025_metadata.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--group-id", action="append", default=[ETHIOPIA_CORE_TEAM_GROUP_ID])
    parser.add_argument("--service-name-suffix", default="")
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
    year = metadata["year"]
    spec = {
        "key": "nighttime_lights",
        "title": f"Ethiopia Black Marble Nighttime Lights {year}",
        "path": metadata["product"]["path"],
        "snippet": f"NASA Black Marble annual nighttime lights composite for Ethiopia, {year}.",
        "description": (
            f"Annual NASA Black Marble {metadata['product_id']} "
            f"{metadata['variable']} nighttime lights raster for Ethiopia, {year}. "
            "Generated with worldbank/blackmarblepy and published through AGOL Copy Raster."
        ),
        "base_tags": ["ethiopia", "geo hub", "eth", "foundational_data", "raster"],
        "tags": [
            "nighttime lights",
            "black marble",
            "nasa",
            metadata["product_id"].lower(),
            str(year),
        ],
    }
    published = publish_spec(gis, portal, token, username, spec, args.group_id, args.service_name_suffix)
    metadata["agol_hosted_imagery_item"] = published
    metadata["agol_publish_method"] = {
        "tool": "arcgis.raster.analytics.copy_raster",
        "raster_type_name": "Raster Dataset",
        "tiles_only": False,
        "input": "local GeoTIFF uploaded to AGOL raster user store",
    }
    args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"updated metadata {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
