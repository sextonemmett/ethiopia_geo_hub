#!/usr/bin/env python3
"""Download Ethiopia market centers from HDX and build an AGOL-ready point layer."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
from typing import Any

import geopandas as gpd
import pandas as pd
import requests


DATASET_ID = "ethiopia-market-centers"
CKAN_PACKAGE_URL = "https://data.humdata.org/api/3/action/package_show"
DEFAULT_CSV = Path("data/market_centers/source/ethiopia_market_centers.csv")
DEFAULT_OUTPUT = Path("data/market_centers/ethiopia_market_centers.geojson")
DEFAULT_METADATA = Path("data/market_centers/ethiopia_market_centers_metadata.json")
USER_AGENT = "ethiopia-geo-hub-market-centers/1.0"


def clean(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def fetch_package(dataset_id: str, timeout: int) -> dict[str, Any]:
    response = requests.get(
        CKAN_PACKAGE_URL,
        params={"id": dataset_id},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"HDX package_show failed for {dataset_id}: {payload}")
    return payload["result"]


def choose_csv_resource(package: dict[str, Any]) -> dict[str, Any]:
    resources = [
        resource
        for resource in package.get("resources", [])
        if clean(resource.get("format")).lower() == "csv" or clean(resource.get("url")).lower().endswith(".csv")
    ]
    if not resources:
        raise RuntimeError("No CSV resource found in the HDX package.")
    return max(resources, key=lambda resource: (parse_timestamp(resource.get("last_modified")), parse_timestamp(resource.get("created"))))


def download_file(url: str, output: Path, timeout: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with tempfile.NamedTemporaryFile("wb", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)
    temp_path.replace(output)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
        temp_path = Path(file.name)
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    temp_path.replace(path)


def load_market_rows(csv_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    required = {"OBJECTID", "Cn_Name", "GlobalID", "ADM3_EN", "ADM3_PCODE", "ADM2_EN", "ADM2_PCODE", "ADM1_EN", "ADM1_PCODE", "x", "y"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"Market center CSV is missing required columns: {sorted(missing)}")
    frame["longitude"] = pd.to_numeric(frame["x"], errors="coerce")
    frame["latitude"] = pd.to_numeric(frame["y"], errors="coerce")
    valid = frame["longitude"].between(32, 49) & frame["latitude"].between(3, 16)
    if not valid.all():
        bad = int((~valid).sum())
        raise RuntimeError(f"Market center CSV has {bad:,} rows with missing or out-of-bounds coordinates.")
    return frame


def build_layer(
    *,
    package: dict[str, Any],
    resource: dict[str, Any],
    csv_path: Path,
    output_path: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    frame = load_market_rows(csv_path)
    points = gpd.GeoDataFrame(
        {
            "source_objectid": frame["OBJECTID"],
            "market_name": frame["Cn_Name"].map(clean),
            "source_globalid": frame["GlobalID"].map(clean),
            "source_adm3_name": frame["ADM3_EN"].map(clean),
            "source_adm3_pcode": frame["ADM3_PCODE"].map(clean),
            "source_adm2_name": frame["ADM2_EN"].map(clean),
            "source_adm2_pcode": frame["ADM2_PCODE"].map(clean),
            "source_adm1_name": frame["ADM1_EN"].map(clean),
            "source_adm1_pcode": frame["ADM1_PCODE"].map(clean),
            "longitude": frame["longitude"],
            "latitude": frame["latitude"],
            "geometry_source": "hdx_market_centers_x_y",
            "source": "HDX Ethiopia - Market Centers",
        },
        geometry=gpd.points_from_xy(frame["longitude"], frame["latitude"]),
        crs="EPSG:4326",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    points.to_file(output_path, driver="GeoJSON")

    metadata = {
        "dataset": package.get("title") or DATASET_ID,
        "dataset_id": package.get("name") or DATASET_ID,
        "package_modified": package.get("metadata_modified"),
        "source_url": f"https://data.humdata.org/dataset/{DATASET_ID}",
        "source_license": package.get("license_title") or package.get("license_id"),
        "resource": {
            "id": resource.get("id"),
            "name": resource.get("name"),
            "url": resource.get("url"),
            "format": resource.get("format"),
            "last_modified": resource.get("last_modified"),
            "created": resource.get("created"),
        },
        "csv": str(csv_path),
        "output": str(output_path),
        "source_feature_count": len(frame),
        "coordinate_feature_count": len(points),
        "source_adm3_pcode_count": int(frame["ADM3_PCODE"].nunique()),
    }
    write_json(metadata_path, metadata)
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", default=DATASET_ID)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package = fetch_package(args.dataset_id, args.timeout)
    resource = choose_csv_resource(package)
    if not args.skip_download:
        download_file(resource["url"], args.csv, args.timeout)
    metadata = build_layer(
        package=package,
        resource=resource,
        csv_path=args.csv,
        output_path=args.output,
        metadata_path=args.metadata,
    )
    print(f"Selected resource: {metadata['resource']['name']}")
    print(f"Wrote {args.csv}")
    print(f"Wrote {args.output} with {metadata['coordinate_feature_count']:,} point features")
    print(f"Wrote {args.metadata}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
