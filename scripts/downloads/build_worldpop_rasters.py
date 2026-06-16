#!/usr/bin/env python3
"""Download and derive Ethiopia WorldPop raster cuts.

The age/sex source rasters are published in 5-year age bands. The requested
non-standard age ranges are derived with proportional splits at partial bands.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import numpy as np
import rasterio
import requests


DEFAULT_YEAR = 2026
DEFAULT_OUTPUT_DIR = Path("data/worldpop")
RELEASE = "R2025A"
VERSION = "v1"
COUNTRY = "ETH"
ISO = "eth"
RESOLUTION = "1km"
RESOLUTION_DIR = "1km_ua"
FILE_SUFFIX = f"CN_{RESOLUTION}_{RELEASE}_UA_{VERSION}"
POPULATION_BASE_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2015_2030/"
    f"{RELEASE}/{{year}}/{COUNTRY}/{VERSION}/{RESOLUTION_DIR}/constrained/"
)
AGE_BASE_URL = (
    "https://data.worldpop.org/GIS/AgeSex_structures/Global_2015_2030/"
    f"{RELEASE}/{{year}}/{COUNTRY}/{VERSION}/{RESOLUTION_DIR}/constrained/"
)

AGE_CUTS: dict[str, dict[str, float]] = {
    # 0-18 inclusive. WorldPop's 15 band represents ages 15-19, so include 4/5.
    "pop_18_and_under": {"00": 1.0, "01": 1.0, "05": 1.0, "10": 1.0, "15": 0.8},
    # 15-64 inclusive. This aligns cleanly to WorldPop's 5-year age bands.
    "pop_15_64": {
        "15": 1.0,
        "20": 1.0,
        "25": 1.0,
        "30": 1.0,
        "35": 1.0,
        "40": 1.0,
        "45": 1.0,
        "50": 1.0,
        "55": 1.0,
        "60": 1.0,
    },
}


def source_urls(year: int) -> dict[str, str]:
    population_base = POPULATION_BASE_URL.format(year=year)
    age_base = AGE_BASE_URL.format(year=year)
    urls = {
        "population_total": urljoin(population_base, f"{ISO}_pop_{year}_{FILE_SUFFIX}.tif")
    }
    age_codes = sorted({age for cut in AGE_CUTS.values() for age in cut})
    for age in age_codes:
        urls[f"age_t_{age}"] = urljoin(age_base, f"{ISO}_t_{age}_{year}_{FILE_SUFFIX}.tif")
    return urls


def download_file(url: str, path: Path, timeout: int, chunk_size: int = 1024 * 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".part")
    existing_size = temp_path.stat().st_size if temp_path.exists() else 0
    headers = {"User-Agent": "ethiopia-geo-hub-worldpop-downloader/1.0"}
    if existing_size:
        headers["Range"] = f"bytes={existing_size}-"

    with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
        if response.status_code == 416 and temp_path.exists():
            temp_path.replace(path)
            return
        if response.status_code not in {200, 206}:
            raise RuntimeError(f"Download failed for {url}: HTTP {response.status_code} {response.text[:200]}")
        mode = "ab" if response.status_code == 206 and existing_size else "wb"
        with temp_path.open(mode) as file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    file.write(chunk)
    temp_path.replace(path)


def _download_one(key: str, url: str, path: Path, timeout: int) -> tuple[str, Path]:
    if path.exists() and path.stat().st_size:
        print(f"exists {path}", flush=True)
        return key, path
    print(f"download {url}", flush=True)
    download_file(url, path, timeout)
    print(f"done {path}", flush=True)
    return key, path


def ensure_downloads(year: int, raw_dir: Path, timeout: int, workers: int) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    jobs = [(key, url, raw_dir / url.rsplit("/", 1)[1]) for key, url in source_urls(year).items()]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_download_one, key, url, path, timeout) for key, url, path in jobs]
        for future in as_completed(futures):
            key, path = future.result()
            paths[key] = path
    return paths


def assert_matching_grid(reference: rasterio.DatasetReader, candidate: rasterio.DatasetReader, path: Path) -> None:
    if (
        reference.width != candidate.width
        or reference.height != candidate.height
        or reference.transform != candidate.transform
        or reference.crs != candidate.crs
    ):
        raise RuntimeError(f"Raster grid mismatch for {path}")


def derive_age_cut(name: str, weights: dict[str, float], paths: dict[str, Path], output_path: Path) -> dict[str, Any]:
    sources = [(age, weight, paths[f"age_t_{age}"]) for age, weight in weights.items()]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(sources[0][2]) as reference:
        profile = reference.profile.copy()
        profile.update(
            driver="GTiff",
            dtype="float32",
            count=1,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            nodata=-99999.0,
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            datasets = [(age, weight, rasterio.open(path)) for age, weight, path in sources]
            try:
                for _, _, dataset in datasets:
                    assert_matching_grid(reference, dataset, Path(dataset.name))
                for _, window in reference.block_windows(1):
                    result = np.zeros((window.height, window.width), dtype="float32")
                    valid = np.zeros((window.height, window.width), dtype=bool)
                    for _, weight, dataset in datasets:
                        data = dataset.read(1, window=window, masked=True).astype("float32")
                        result += np.ma.filled(data, 0.0) * weight
                        valid |= ~np.ma.getmaskarray(data)
                    result[~valid] = profile["nodata"]
                    dst.write(result, 1, window=window)
            finally:
                for _, _, dataset in datasets:
                    dataset.close()

    return {
        "path": str(output_path),
        "weights": weights,
        "method": "weighted sum of WorldPop total-sex age bands; partial 5-year bands split proportionally",
    }


def raster_summary(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        return {
            "path": str(path),
            "width": dataset.width,
            "height": dataset.height,
            "crs": str(dataset.crs),
            "bounds": list(dataset.bounds),
            "dtype": dataset.dtypes[0],
            "nodata": dataset.nodata,
        }


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, help=f"WorldPop target year. Default: {DEFAULT_YEAR}")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds. Default: 120")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent download workers. Default: 4")
    parser.add_argument("--download-only", action="store_true", help="Download sources but do not derive age-cut rasters.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_dir = args.output_dir / "raw" / str(args.year)
    derived_dir = args.output_dir / "derived" / str(args.year)
    paths = ensure_downloads(args.year, raw_dir, args.timeout, args.workers)

    derived: dict[str, Any] = {}
    if not args.download_only:
        for name, weights in AGE_CUTS.items():
            output_path = derived_dir / f"eth_worldpop_{name}_{args.year}_{FILE_SUFFIX}.tif"
            print(f"derive {output_path}")
            derived[name] = derive_age_cut(name, weights, paths, output_path)

    products = {
        "population_total": raster_summary(paths["population_total"]),
        **{name: raster_summary(Path(info["path"])) for name, info in derived.items()},
    }
    metadata = {
        "country": COUNTRY,
        "iso": ISO,
        "year": args.year,
        "release": RELEASE,
        "version": VERSION,
        "resolution": RESOLUTION,
        "resolution_directory": RESOLUTION_DIR,
        "file_suffix": FILE_SUFFIX,
        "source_urls": source_urls(args.year),
        "derived": derived,
        "products": products,
    }
    metadata_path = args.output_dir / f"worldpop_ethiopia_{args.year}_metadata.json"
    write_metadata(metadata_path, metadata)
    print(f"metadata {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
