#!/usr/bin/env python3
"""Download Ookla Speedtest tiles and clip them to Ethiopia."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any
import zipfile

import geopandas as gpd
import pandas as pd
import requests
import shapefile
from shapely.geometry import shape


BUCKET_URL = "https://ookla-open-data.s3.us-west-2.amazonaws.com"
DEFAULT_YEAR = 2026
DEFAULT_QUARTER = 1
DEFAULT_TYPES = ["fixed", "mobile"]
DEFAULT_BOUNDARY = Path("data/admin_boundaries/boundaries_1.geojson")
DEFAULT_OUTPUT_DIR = Path("data/connectivity/ookla")
DEFAULT_SOURCE_DIR = DEFAULT_OUTPUT_DIR / "source"
DEFAULT_METADATA = DEFAULT_OUTPUT_DIR / "ookla_speedtest_ethiopia_2026_q1_metadata.json"
USER_AGENT = "ethiopia-geo-hub-ookla-speedtest/1.0"


def quarter_start(year: int, quarter: int) -> date:
    if quarter not in {1, 2, 3, 4}:
        raise ValueError(f"Quarter must be 1-4, got {quarter}.")
    return date(year, (quarter - 1) * 3 + 1, 1)


def source_key(service_type: str, year: int, quarter: int) -> str:
    start = quarter_start(year, quarter)
    return (
        f"shapefiles/performance/type={service_type}/year={year}/quarter={quarter}/"
        f"{start.isoformat()}_performance_{service_type}_tiles.zip"
    )


def source_url(service_type: str, year: int, quarter: int) -> str:
    return f"{BUCKET_URL}/{source_key(service_type, year, quarter)}"


def clean(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def download_file(url: str, output: Path, timeout: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(
        url,
        headers={"Accept": "*/*", "User-Agent": USER_AGENT},
        stream=True,
        timeout=timeout,
    ) as response:
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


def zip_shapefile_base(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        shp_names = [name for name in archive.namelist() if name.lower().endswith(".shp")]
    if len(shp_names) != 1:
        raise RuntimeError(f"Expected one .shp in {path}, found {len(shp_names)}.")
    return shp_names[0][:-4]


def bbox_intersects(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return left[0] <= right[2] and left[2] >= right[0] and left[1] <= right[3] and left[3] >= right[1]


def read_bbox_candidates(path: Path, bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    base = zip_shapefile_base(path)
    with zipfile.ZipFile(path) as archive:
        with (
            archive.open(f"{base}.shp") as shp,
            archive.open(f"{base}.shx") as shx,
            archive.open(f"{base}.dbf") as dbf,
        ):
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
            fields = [field[0] for field in reader.fields[1:]]
            records: list[dict[str, Any]] = []
            geometries = []
            for item in reader.iterShapeRecords():
                if not bbox_intersects(tuple(item.shape.bbox), bbox):
                    continue
                props = dict(zip(fields, item.record, strict=True))
                records.append(props)
                geometries.append(shape(item.shape.__geo_interface__))

    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def load_ethiopia_boundary(path: Path) -> gpd.GeoDataFrame:
    boundary = gpd.read_file(path).to_crs("EPSG:4326")
    if boundary.empty:
        raise RuntimeError(f"Boundary file has no features: {path}")
    dissolved = boundary[["geometry"]].dissolve()
    dissolved["clip_name"] = "Ethiopia"
    return dissolved


def normalize_columns(frame: gpd.GeoDataFrame, service_type: str, year: int, quarter: int) -> gpd.GeoDataFrame:
    renamed = {
        "avg_d_kbps": "avg_download_kbps",
        "avg_u_kbps": "avg_upload_kbps",
        "avg_lat_ms": "avg_latency_ms",
        "avg_lat_do": "avg_loaded_download_latency_ms",
        "avg_lat_up": "avg_loaded_upload_latency_ms",
    }
    frame = frame.rename(columns={key: value for key, value in renamed.items() if key in frame.columns})
    frame["service_type"] = service_type
    frame["year"] = year
    frame["quarter"] = quarter
    frame["period_start"] = quarter_start(year, quarter).isoformat()
    frame["source"] = "Speedtest by Ookla Global Fixed and Mobile Network Performance Maps"
    preferred = [
        "service_type",
        "year",
        "quarter",
        "period_start",
        "quadkey",
        "avg_download_kbps",
        "avg_upload_kbps",
        "avg_latency_ms",
        "avg_loaded_download_latency_ms",
        "avg_loaded_upload_latency_ms",
        "tests",
        "devices",
        "tile_x",
        "tile_y",
        "source",
        "geometry",
    ]
    columns = [column for column in preferred if column in frame.columns]
    columns.extend(column for column in frame.columns if column not in columns)
    return frame[columns]


def build_service_type(
    *,
    service_type: str,
    year: int,
    quarter: int,
    source_dir: Path,
    output_dir: Path,
    boundary: gpd.GeoDataFrame,
    skip_download: bool,
    timeout: int,
) -> dict[str, Any]:
    url = source_url(service_type, year, quarter)
    zip_path = source_dir / Path(source_key(service_type, year, quarter)).name
    output_path = output_dir / f"ethiopia_ookla_speedtest_{service_type}_{year}_q{quarter}.geojson"

    if not skip_download or not zip_path.exists():
        print(f"Downloading {url}", flush=True)
        download_file(url, zip_path, timeout)

    bbox = tuple(boundary.total_bounds)
    candidates = read_bbox_candidates(zip_path, bbox)  # type: ignore[arg-type]
    if candidates.empty:
        raise RuntimeError(f"No {service_type} source tiles intersect Ethiopia bbox.")

    clipped = gpd.clip(candidates, boundary)
    clipped = clipped[~clipped.geometry.is_empty & clipped.geometry.notna()].copy()
    clipped = normalize_columns(clipped, service_type, year, quarter)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    clipped.to_file(output_path, driver="GeoJSON")

    return {
        "service_type": service_type,
        "source_url": url,
        "source_s3_key": source_key(service_type, year, quarter),
        "source_zip": str(zip_path),
        "output": str(output_path),
        "bbox_candidate_feature_count": int(len(candidates)),
        "ethiopia_clipped_feature_count": int(len(clipped)),
        "bounds": [float(value) for value in clipped.total_bounds],
        "columns": [str(column) for column in clipped.columns if column != "geometry"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--quarter", type=int, default=DEFAULT_QUARTER)
    parser.add_argument("--type", choices=["fixed", "mobile"], action="append", dest="types", default=[])
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service_types = args.types or DEFAULT_TYPES
    boundary = load_ethiopia_boundary(args.boundary)

    products = {}
    for service_type in service_types:
        products[service_type] = build_service_type(
            service_type=service_type,
            year=args.year,
            quarter=args.quarter,
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            boundary=boundary,
            skip_download=args.skip_download,
            timeout=args.timeout,
        )
        print(
            f"Wrote {products[service_type]['output']} with "
            f"{products[service_type]['ethiopia_clipped_feature_count']:,} clipped features",
            flush=True,
        )

    metadata = {
        "dataset": "Speedtest by Ookla Global Fixed and Mobile Network Performance Maps",
        "source_marketplace_url": "https://aws.amazon.com/marketplace/pp/prodview-u33jxric374io",
        "source_registry_url": "https://registry.opendata.aws/speedtest-global-performance",
        "source_github_url": "https://github.com/teamookla/ookla-open-data",
        "source_license": "Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International",
        "year": args.year,
        "quarter": args.quarter,
        "period_start": quarter_start(args.year, args.quarter).isoformat(),
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "boundary": str(args.boundary),
        "boundary_feature_count": int(len(boundary)),
        "products": products,
    }
    write_json(args.metadata, metadata)
    print(f"Wrote {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
