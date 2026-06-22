#!/usr/bin/env python3
"""Create EPSG:4326 copies of market accessibility rasters for AGOL publishing."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject


DEFAULT_METADATA = Path("data/market_accessibility/ethiopia_market_accessibility_500m_metadata.json")
NODATA = -9999.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    return parser.parse_args()


def build_wgs84_copy(source_path: Path) -> dict[str, object]:
    output_path = source_path.with_name(f"{source_path.stem}_wgs84.tif")
    with rasterio.open(source_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs,
            "EPSG:4326",
            src.width,
            src.height,
            *src.bounds,
        )
        profile = src.profile.copy()
        profile.update(
            crs="EPSG:4326",
            transform=transform,
            width=width,
            height=height,
            nodata=NODATA,
            dtype="float32",
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=transform,
                dst_crs="EPSG:4326",
                dst_nodata=NODATA,
                resampling=Resampling.bilinear,
            )

    with rasterio.open(output_path) as dst:
        return {
            "path": str(output_path),
            "crs": str(dst.crs),
            "width": dst.width,
            "height": dst.height,
            "bounds": list(dst.bounds),
            "pixel_size_x": abs(dst.res[0]),
            "pixel_size_y": abs(dst.res[1]),
            "dtype": dst.dtypes[0],
            "nodata": dst.nodata,
        }


def main() -> int:
    args = parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    staged = {}
    for mode in ("walk", "least_cost"):
        staged[mode] = build_wgs84_copy(Path(metadata["outputs"][mode]["accessibility"]))
        metadata["outputs"][mode]["accessibility_wgs84"] = staged[mode]["path"]
        print(f"wrote {staged[mode]['path']}", flush=True)
    metadata["agol_wgs84_accessibility_rasters"] = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "purpose": "EPSG:4326 copies for AGOL hosted imagery publishing and Map Viewer compatibility.",
        "rasters": staged,
    }
    args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"updated {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
