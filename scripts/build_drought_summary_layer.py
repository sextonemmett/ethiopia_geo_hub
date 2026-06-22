#!/usr/bin/env python3
"""Build a one-layer Ethiopia drought summary from clipped GDO RDRI rasters."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import bounds as window_bounds
from rasterio.windows import Window
from shapely.geometry import mapping, box


DEFAULT_DROUGHT_DIR = Path("data/hazards/drought")
DEFAULT_OUTPUT = Path("data/hazards/drought/ethiopia_drought_summary_2020_2025.geojson")
DEFAULT_METADATA = Path("data/hazards/drought/ethiopia_drought_summary_2020_2025_metadata.json")
NODATA = -9999.0


def date_from_name(path: Path) -> str:
    match = re.search(r"_(20\d{6})_", path.name)
    if not match:
        raise ValueError(f"Could not parse date from {path.name}")
    return match.group(1)


def label_date(value: str | None) -> str:
    if not value:
        return ""
    return datetime.strptime(value, "%Y%m%d").date().isoformat()


def drought_paths(root: Path) -> list[Path]:
    paths = sorted(
        path
        for path in root.glob("rdria_m_gdo_*_t/*.tif")
        if re.search(r"_(20\d{6})_", path.name)
    )
    if not paths:
        raise FileNotFoundError(f"No clipped drought TIFFs found under {root}")
    return sorted(paths, key=date_from_name)


def summarize_values(values_by_date: list[tuple[str, float]]) -> dict[str, Any]:
    classes = [(date, int(value)) for date, value in values_by_date if np.isfinite(value) and value != NODATA]
    if not classes:
        raise ValueError("Cannot summarize an empty value series")

    values = [value for _, value in classes]
    drought_dates = [(date, value) for date, value in classes if value >= 1]
    first_date, first_value = classes[0]
    latest_date, latest_value = classes[-1]
    modal_class = Counter(values).most_common(1)[0][0]

    result: dict[str, Any] = {
        "valid_periods": len(values),
        "max_class": max(values),
        "latest_class": latest_value,
        "latest_date": label_date(latest_date),
        "modal_class": modal_class,
        "drought_periods": sum(value >= 1 for value in values),
        "moderate_periods": sum(value >= 2 for value in values),
        "severe_periods": sum(value >= 3 for value in values),
        "first_valid": label_date(first_date),
        "first_class": first_value,
        "first_drought": label_date(drought_dates[0][0]) if drought_dates else "",
        "last_drought": label_date(drought_dates[-1][0]) if drought_dates else "",
        "trend": latest_value - first_value,
    }

    years = sorted({date[:4] for date, _ in classes})
    for year in years:
        year_values = [value for date, value in classes if date.startswith(year)]
        result[f"max_{year}"] = max(year_values)
        result[f"drought_{year}"] = sum(value >= 1 for value in year_values)
        result[f"severe_{year}"] = sum(value >= 3 for value in year_values)
    return result


def build_summary(drought_dir: Path, output_path: Path, metadata_path: Path) -> None:
    paths = drought_paths(drought_dir)
    dates = [date_from_name(path) for path in paths]

    arrays: list[np.ndarray] = []
    profile: dict[str, Any] | None = None
    for path in paths:
        with rasterio.open(path) as dataset:
            if profile is None:
                profile = {
                    "width": dataset.width,
                    "height": dataset.height,
                    "transform": dataset.transform,
                    "bounds": dataset.bounds,
                    "crs": str(dataset.crs),
                    "pixel_size_x": abs(dataset.res[0]),
                    "pixel_size_y": abs(dataset.res[1]),
                    "nodata": dataset.nodata,
                }
            elif dataset.width != profile["width"] or dataset.height != profile["height"] or dataset.transform != profile["transform"]:
                raise RuntimeError(f"{path} does not align with the first raster")
            arrays.append(dataset.read(1))

    assert profile is not None
    stack = np.stack(arrays)
    valid_any = np.any((stack != NODATA) & np.isfinite(stack), axis=0)
    features = []
    for row in range(profile["height"]):
        for col in range(profile["width"]):
            if not valid_any[row, col]:
                continue
            series = [(date, float(stack[index, row, col])) for index, date in enumerate(dates)]
            properties = summarize_values(series)
            properties["cell_id"] = f"r{row:02d}_c{col:02d}"
            properties["grid_row"] = row
            properties["grid_col"] = col
            xmin, ymin, xmax, ymax = window_bounds(Window(col, row, 1, 1), profile["transform"])
            features.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": mapping(box(xmin, ymin, xmax, ymax)),
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "type": "FeatureCollection",
        "name": output_path.stem,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    output_path.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")

    metadata = {
        "dataset": "Global Drought Observatory RDRI drought class summary",
        "source_provider": "European Union / European Commission",
        "source_license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
        "source_paths": [str(path) for path in paths],
        "date_start": label_date(dates[0]),
        "date_end": label_date(dates[-1]),
        "period_count": len(paths),
        "feature_count": len(features),
        "source_profile": profile,
        "summary_fields": {
            "max_class": "Highest drought class observed across all periods.",
            "latest_class": "Drought class on the latest available period.",
            "modal_class": "Most frequent drought class across all valid periods.",
            "drought_periods": "Count of valid periods with class >= 1.",
            "moderate_periods": "Count of valid periods with class >= 2.",
            "severe_periods": "Count of valid periods with class >= 3.",
            "first_drought": "First valid period with class >= 1.",
            "last_drought": "Last valid period with class >= 1.",
            "trend": "latest_class minus first_class.",
        },
        "processing": {
            "clip": "Source TIFFs were clipped in place to the Ethiopia boundary dissolved from admin-1 polygons before summarization.",
            "boundary": "data/admin_boundaries/boundaries_1.geojson",
            "nodata": NODATA,
        },
        "output": str(output_path),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output_path} ({len(features)} features)")
    print(f"wrote {metadata_path}")


def main() -> int:
    build_summary(DEFAULT_DROUGHT_DIR, DEFAULT_OUTPUT, DEFAULT_METADATA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
