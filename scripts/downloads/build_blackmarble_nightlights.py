#!/usr/bin/env python3
"""Download Ethiopia annual NASA Black Marble nighttime lights raster."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import geopandas as gpd
import rasterio
from rasterio.enums import Resampling

from blackmarble import BlackMarble, Product


DEFAULT_YEAR = 2025
DEFAULT_ADM1 = Path("data/admin_boundaries/boundaries_1.geojson")
DEFAULT_OUTPUT_DIR = Path("data/nighttime_lights")
PRODUCT = Product.VNP46A4
VARIABLE = "NearNadir_Composite_Snow_Free"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def ethiopia_roi(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    gdf = gdf.to_crs("EPSG:4326")
    dissolved = gdf.dissolve()
    return gpd.GeoDataFrame({"name": ["Ethiopia"]}, geometry=[dissolved.geometry.iloc[0]], crs="EPSG:4326")


def write_cog_like_geotiff(dataset: Any, variable: str, output_path: Path) -> dict[str, Any]:
    data_array = dataset[variable].squeeze(drop=True)
    if not data_array.rio.crs:
        data_array = data_array.rio.write_crs("EPSG:4326")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_array.rio.to_raster(
        output_path,
        driver="GTiff",
        dtype="float32",
        compress="deflate",
        predictor=3,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        BIGTIFF="IF_SAFER",
    )
    with rasterio.open(output_path, "r+") as dst:
        # Build internal overviews for faster AGOL ingestion and map rendering.
        factors = [2, 4, 8, 16]
        dst.build_overviews(factors, Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    with rasterio.open(output_path) as raster:
        return {
            "path": str(output_path),
            "width": raster.width,
            "height": raster.height,
            "crs": str(raster.crs),
            "bounds": list(raster.bounds),
            "dtype": raster.dtypes[0],
            "nodata": raster.nodata,
            "pixel_size_x": abs(raster.res[0]),
            "pixel_size_y": abs(raster.res[1]),
        }


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--adm1", type=Path, default=DEFAULT_ADM1)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()
    token = os.getenv("BLACKMARBLE_TOKEN", "").strip() or os.getenv("EARTHDATA_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Set BLACKMARBLE_TOKEN or EARTHDATA_TOKEN in .env or your shell. "
            "Create a NASA Earthdata token at https://urs.earthdata.nasa.gov/profile."
        )

    raw_dir = args.output_dir / "raw" / str(args.year)
    product_dir = args.output_dir / "products" / str(args.year)
    output_path = product_dir / f"ethiopia_blackmarble_{PRODUCT.value}_{VARIABLE}_{args.year}.tif"
    metadata_path = args.output_dir / f"ethiopia_blackmarble_{args.year}_metadata.json"

    roi = ethiopia_roi(args.adm1)
    bm = BlackMarble(token=token, output_directory=raw_dir, output_skip_if_exists=True)
    dataset = bm.raster(
        roi,
        product_id=PRODUCT,
        date_range=dt.date(args.year, 1, 1),
        variable=VARIABLE,
    )
    product = write_cog_like_geotiff(dataset, VARIABLE, output_path)
    metadata = {
        "country": "ETH",
        "year": args.year,
        "source": "NASA Black Marble",
        "library": "worldbank/blackmarblepy",
        "product_id": PRODUCT.value,
        "variable": VARIABLE,
        "description": "Annual NASA Black Marble nighttime lights composite for Ethiopia.",
        "adm1_source": str(args.adm1),
        "product": product,
    }
    write_metadata(metadata_path, metadata)
    print(f"wrote {output_path}")
    print(f"metadata {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
