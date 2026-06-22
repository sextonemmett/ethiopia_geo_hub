#!/usr/bin/env python3
"""Build Ethiopia-clipped MapSPAM top crop production rasters."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
from zipfile import ZipFile, is_zipfile

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.mask import mask
import requests
from shapely.geometry import mapping


DEFAULT_OUTPUT_DIR = Path("data/mapspam")
DEFAULT_BOUNDARY = Path("data/admin_boundaries/boundaries_1.geojson")
DEFAULT_TOP_N = 10
YEAR = 2020
RELEASE = "SPAM 2020 v2.2"
VERSION = "V2r2"
COUNTRY = "ETH"
VARIABLE = "production"
VARIABLE_CODE = "P"
TECHNOLOGY_CODE = "TA"
GEOTIFF_TECHNOLOGY_CODE = "A"
TECHNOLOGY_NAME = "all technologies together"
MAPSPAM_DATA_URL = "https://www.mapspam.info/data/"
DOI = "10.7910/DVN/SWPENT"
GEOTIFF_ZIP_URL = (
    "https://www.dropbox.com/scl/fi/euolvnpdooxhd5ljskl4q/"
    "spam2020V2r2_global_production.geotiff.zip?dl=1&rlkey=mttc09xhfr4m1zsd4ik40wvz1"
)
README_URL = (
    "https://www.dropbox.com/scl/fi/n5qyf8uk2ylwg8zmvbmng/"
    "Readme_SPAM2020V2r2.txt?dl=1&rlkey=riphyjg4k2drkvt8ak9l8g4jk"
)
USER_AGENT = "ethiopia-geo-hub-mapspam-downloader/1.0"

CROPS: dict[str, str] = {
    "whea": "Wheat",
    "rice": "Rice",
    "maiz": "Maize",
    "barl": "Barley",
    "mill": "Small Millet",
    "pmil": "Pearl Millet",
    "sorg": "Sorghum",
    "ocer": "Other Cereals",
    "pota": "Potato",
    "swpo": "Sweet Potato",
    "yams": "Yams",
    "cass": "Cassava",
    "orts": "Other Roots",
    "bean": "Bean",
    "chic": "Chickpea",
    "cowp": "Cowpea",
    "pige": "Pigeon Pea",
    "lent": "Lentil",
    "opul": "Other Pulses",
    "soyb": "Soybean",
    "grou": "Groundnut",
    "cnut": "Coconut",
    "oilp": "Oilpalm",
    "sunf": "Sunflower",
    "rape": "Rapeseed",
    "sesa": "Sesame Seed",
    "ooil": "Other Oil Crops",
    "sugc": "Sugarcane",
    "sugb": "Sugarbeet",
    "cott": "Cotton",
    "ofib": "Other Fibre Crops",
    "coff": "Arabic Coffee",
    "rcof": "Robust Coffee",
    "coco": "Cocoa",
    "teas": "Tea",
    "toba": "Tobacco",
    "bana": "Banana",
    "plnt": "Plantain",
    "citr": "Citrus",
    "trof": "Other Tropical Fruit",
    "temf": "Temperate Fruit",
    "toma": "Tomato",
    "onio": "Onion",
    "vege": "Other Vegetables",
    "rubb": "Rubber",
    "rest": "Rest",
}


def download_file(url: str, path: Path, timeout: int, refresh: bool = False) -> None:
    if path.exists() and path.stat().st_size and not refresh:
        print(f"exists {path}", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".part")
    if temp_path.exists():
        temp_path.unlink()
    print(f"download {url}", flush=True)
    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=timeout,
        allow_redirects=True,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Download failed for {url}: HTTP {response.status_code} {response.text[:200]}")
        with temp_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)
    temp_path.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zip(path: Path) -> None:
    if not path.exists() or not path.stat().st_size:
        raise FileNotFoundError(path)
    if not is_zipfile(path):
        head = path.read_bytes()[:80]
        raise RuntimeError(f"{path} is not a ZIP archive. First bytes: {head!r}")
    with ZipFile(path) as archive:
        bad = archive.testzip()
    if bad:
        raise RuntimeError(f"{path} failed ZIP validation at member {bad}")


def crop_code_from_member(member: str) -> str | None:
    name = Path(member).name.lower()
    if not name.endswith((".tif", ".tiff")):
        return None
    pattern = rf"_{VARIABLE_CODE.lower()}_([a-z]{{4}})_{GEOTIFF_TECHNOLOGY_CODE.lower()}\.tiff?$"
    match = re.search(pattern, name)
    if match and match.group(1) in CROPS:
        return match.group(1)
    for code in CROPS:
        if f"_{VARIABLE_CODE.lower()}_{code}_{GEOTIFF_TECHNOLOGY_CODE.lower()}" in name:
            return code
    return None


def geotiff_members(zip_path: Path) -> dict[str, str]:
    members: dict[str, str] = {}
    with ZipFile(zip_path) as archive:
        for member in archive.namelist():
            code = crop_code_from_member(member)
            if code:
                members[code] = member
    missing = sorted(set(CROPS) - set(members))
    if missing:
        with ZipFile(zip_path) as archive:
            sample = "\n".join(archive.namelist()[:25])
        raise RuntimeError(
            f"Missing {len(missing)} {VARIABLE_CODE}_{TECHNOLOGY_CODE} crop GeoTIFFs in {zip_path}: "
            f"{missing}. Archive starts with:\n{sample}"
        )
    return members


def dissolve_boundary(path: Path, crs: Any) -> list[dict[str, Any]]:
    boundary = gpd.read_file(path)
    if boundary.empty:
        raise RuntimeError(f"No boundary features found in {path}")
    if boundary.crs is None:
        boundary = boundary.set_crs("EPSG:4326")
    boundary = boundary.to_crs(crs or "EPSG:4326")
    try:
        dissolved = boundary.geometry.union_all()
    except AttributeError:
        dissolved = boundary.unary_union
    return [mapping(dissolved)]


def masked_values(dataset: rasterio.DatasetReader, geometries: list[dict[str, Any]]) -> np.ndarray:
    data, _ = mask(dataset, geometries, crop=True, filled=False)
    values = np.ma.asarray(data[0])
    compressed = values.compressed().astype("float64", copy=False)
    finite = np.isfinite(compressed)
    non_negative = compressed >= 0
    return compressed[finite & non_negative]


def rank_crops(zip_path: Path, members: dict[str, str], boundary_path: Path) -> list[dict[str, Any]]:
    ranking: list[dict[str, Any]] = []
    for index, code in enumerate(sorted(members), start=1):
        uri = f"zip://{zip_path.resolve()}!{members[code]}"
        with rasterio.open(uri) as dataset:
            geometries = dissolve_boundary(boundary_path, dataset.crs)
            values = masked_values(dataset, geometries)
            ranking.append(
                {
                    "crop_code": code,
                    "crop_name": CROPS[code],
                    "ethiopia_total_production": float(values.sum()),
                    "valid_pixel_count": int(values.size),
                    "source_member": members[code],
                }
            )
        print(f"ranked {index:02d}/{len(members)} {code} {CROPS[code]}", flush=True)
    ranking.sort(key=lambda item: item["ethiopia_total_production"], reverse=True)
    for rank, item in enumerate(ranking, start=1):
        item["rank"] = rank
    return ranking


def extract_member(zip_path: Path, member: str, output_path: Path) -> Path:
    if output_path.exists() and output_path.stat().st_size:
        return output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    if temp_path.exists():
        temp_path.unlink()
    with ZipFile(zip_path) as archive:
        with archive.open(member) as source, temp_path.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
    temp_path.replace(output_path)
    return output_path


def raster_summary(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        values = dataset.read(1, masked=True)
        valid = values.compressed()
        valid = valid[np.isfinite(valid)]
        valid = valid[valid >= 0]
        return {
            "path": str(path),
            "width": dataset.width,
            "height": dataset.height,
            "crs": str(dataset.crs),
            "bounds": list(dataset.bounds),
            "dtype": dataset.dtypes[0],
            "nodata": dataset.nodata,
            "pixel_size_x": abs(dataset.res[0]),
            "pixel_size_y": abs(dataset.res[1]),
            "valid_pixel_count": int(valid.size),
            "total_production": float(valid.astype("float64", copy=False).sum()),
        }


def output_profile(source: rasterio.DatasetReader, data: np.ma.MaskedArray, transform: Any) -> dict[str, Any]:
    nodata = source.nodata
    if nodata is None or not np.isfinite(nodata) or nodata >= 0:
        nodata = -99999.0
    height, width = data.shape
    profile = source.profile.copy()
    profile.update(
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=source.crs or "EPSG:4326",
        transform=transform,
        nodata=float(nodata),
        compress="deflate",
        predictor=3,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        BIGTIFF="IF_SAFER",
    )
    return profile


def clip_raster(source_path: Path, boundary_path: Path, output_path: Path, rebuild: bool = False) -> dict[str, Any]:
    if output_path.exists() and output_path.stat().st_size and not rebuild:
        print(f"exists {output_path}", flush=True)
        return raster_summary(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    if temp_path.exists():
        temp_path.unlink()

    with rasterio.open(source_path) as source:
        geometries = dissolve_boundary(boundary_path, source.crs)
        clipped, transform = mask(source, geometries, crop=True, filled=False)
        band = np.ma.asarray(clipped[0])
        profile = output_profile(source, band, transform)
        data = np.ma.filled(band.astype("float32"), profile["nodata"])
        data[~np.isfinite(data)] = profile["nodata"]
        data[data < 0] = profile["nodata"]
        with rasterio.open(temp_path, "w", **profile) as target:
            target.write(data, 1)
            factors = [factor for factor in (2, 4, 8, 16) if target.width // factor >= 1 and target.height // factor >= 1]
            if factors:
                target.build_overviews(factors, Resampling.average)
                target.update_tags(ns="rio_overview", resampling="average")
    temp_path.replace(output_path)
    return raster_summary(output_path)


def products_for_top_crops(
    zip_path: Path,
    members: dict[str, str],
    ranking: Iterable[dict[str, Any]],
    boundary_path: Path,
    raw_geotiff_dir: Path,
    product_dir: Path,
    rebuild: bool,
) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for item in ranking:
        code = item["crop_code"]
        source_name = Path(members[code]).name
        source_path = extract_member(zip_path, members[code], raw_geotiff_dir / source_name)
        output_path = product_dir / f"eth_mapspam_2020_v2r2_production_{code}_ta.tif"
        print(f"clip {code} {CROPS[code]} -> {output_path}", flush=True)
        summary = clip_raster(source_path, boundary_path, output_path, rebuild=rebuild)
        products[code] = {
            "key": f"production_{code}_ta",
            "rank": item["rank"],
            "crop_code": code,
            "crop_name": CROPS[code],
            "technology_code": TECHNOLOGY_CODE,
            "geotiff_technology_code": GEOTIFF_TECHNOLOGY_CODE,
            "technology_name": TECHNOLOGY_NAME,
            "variable_code": VARIABLE_CODE,
            "variable": VARIABLE,
            "source_member": members[code],
            "global_source_path": str(source_path),
            **summary,
        }
    return products


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--refresh-downloads", action="store_true")
    parser.add_argument("--rebuild-products", action="store_true")
    parser.add_argument("--download-only", action="store_true", help="Download and validate the source ZIP without ranking/clipping.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top_n < 1 or args.top_n > len(CROPS):
        raise SystemExit(f"--top-n must be between 1 and {len(CROPS)}")

    raw_dir = args.output_dir / "raw" / "2020_v2r2"
    raw_geotiff_dir = raw_dir / "geotiff"
    product_dir = args.output_dir / "products" / "2020_v2r2"
    metadata_path = args.output_dir / "mapspam_2020_v2_2_ethiopia_production_metadata.json"
    zip_path = raw_dir / "spam2020V2r2_global_production.geotiff.zip"

    download_file(GEOTIFF_ZIP_URL, zip_path, args.timeout, refresh=args.refresh_downloads)
    validate_zip(zip_path)
    print(f"validated {zip_path}", flush=True)
    if args.download_only:
        return 0

    members = geotiff_members(zip_path)
    ranking = rank_crops(zip_path, members, args.boundary)
    selected = ranking[: args.top_n]
    products = products_for_top_crops(
        zip_path,
        members,
        selected,
        args.boundary,
        raw_geotiff_dir,
        product_dir,
        args.rebuild_products,
    )
    metadata = {
        "dataset": f"{RELEASE} Global Production",
        "country": COUNTRY,
        "year": YEAR,
        "release": RELEASE,
        "version": VERSION,
        "source_page": MAPSPAM_DATA_URL,
        "source_doi": DOI,
        "citation": (
            "International Food Policy Research Institute (IFPRI), 2026, "
            '"Global Spatially-Disaggregated Crop Production Statistics Data for 2020 Version 2.0 Release 2", '
            "https://doi.org/10.7910/DVN/SWPENT, Harvard Dataverse, V5"
        ),
        "variable": VARIABLE,
        "variable_code": VARIABLE_CODE,
        "technology_code": TECHNOLOGY_CODE,
        "geotiff_technology_code": GEOTIFF_TECHNOLOGY_CODE,
        "technology_name": TECHNOLOGY_NAME,
        "unit": "SPAM production pixel values as published",
        "boundary": str(args.boundary),
        "rank_method": (
            "All 46 SPAM total-production GeoTIFFs were masked to the dissolved Ethiopia admin-1 boundary, "
            "summed by crop, sorted descending, and the top crops were clipped to Ethiopia GeoTIFF products."
        ),
        "top_n": args.top_n,
        "downloads": {
            "geotiff_zip": {
                "url": GEOTIFF_ZIP_URL,
                "path": str(zip_path),
                "size_bytes": zip_path.stat().st_size,
                "sha256": sha256(zip_path),
            },
            "readme_url": README_URL,
        },
        "crop_ranking": ranking,
        "selected_crop_codes": [item["crop_code"] for item in selected],
        "products": products,
    }
    write_metadata(metadata_path, metadata)
    print("top crops:", ", ".join(f"{item['rank']}. {item['crop_name']}" for item in selected), flush=True)
    print(f"metadata {metadata_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
