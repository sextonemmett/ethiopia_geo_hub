#!/usr/bin/env python3
"""Build alternate Ethiopia market isochrone band GeoJSONs from accessibility rasters."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape


DEFAULT_METADATA = Path("data/market_accessibility/ethiopia_market_accessibility_500m_metadata.json")

COARSE_BANDS = [
    (0, 30, "0-30 min"),
    (30, 60, "30-60 min"),
    (60, 120, "1-2 hours"),
    (120, 180, "2-3 hours"),
    (180, math.inf, "3+ hours"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--scheme", default="coarse", choices=["coarse"])
    return parser.parse_args()


def build_isochrones(raster_path: Path, mode: str, output_path: Path, bands: list[tuple[int, float, str]]) -> int:
    with rasterio.open(raster_path) as src:
        values = src.read(1, masked=True).filled(np.nan).astype("float32")
        valid = np.isfinite(values) & (values >= 0)
        band_array = np.zeros(values.shape, dtype="uint8")
        for index, (lower, upper, _) in enumerate(bands, start=1):
            if math.isinf(upper):
                mask = valid & (values >= lower)
            else:
                mask = valid & (values >= lower) & (values < upper)
            band_array[mask] = index

        features: list[dict[str, Any]] = []
        band_lookup = {index: band for index, band in enumerate(bands, start=1)}
        for geom, value in shapes(band_array, mask=band_array > 0, transform=src.transform):
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
        gdf = gpd.GeoDataFrame(features, crs=src.crs)

    dissolved = gdf.dissolve(by=["mode", "band", "min_minutes", "max_minutes"], as_index=False)
    dissolved.loc[dissolved["max_minutes"] < 0, "max_minutes"] = None
    dissolved = dissolved.to_crs("EPSG:4326")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dissolved.to_file(output_path, driver="GeoJSON")
    return len(dissolved)


def main() -> int:
    args = parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    suffix = f"{args.scheme}_500m"
    outputs = metadata["outputs"]

    for mode in ("walk", "least_cost"):
        raster_path = Path(outputs[mode]["accessibility"])
        output_path = raster_path.with_name(f"ethiopia_market_isochrones_{mode}_{suffix}.geojson")
        count = build_isochrones(raster_path, mode, output_path, COARSE_BANDS)
        outputs[mode][f"isochrones_{args.scheme}"] = str(output_path)
        outputs[mode][f"isochrone_{args.scheme}_feature_count"] = count
        print(f"wrote {output_path} ({count} features)", flush=True)

    metadata.setdefault("isochrone_band_schemes", {})[args.scheme] = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "bands": [
            {"label": label, "min_minutes": lower, "max_minutes": None if math.isinf(upper) else upper}
            for lower, upper, label in COARSE_BANDS
        ],
    }
    args.metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"updated {args.metadata}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
