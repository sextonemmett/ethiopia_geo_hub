#!/usr/bin/env python3
"""Download Ethiopia MFR health facility coordinates and join to ADM3 polygons."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any
import zipfile
from xml.etree.ElementTree import iterparse

import geopandas as gpd
import pandas as pd
import requests


DEFAULT_EXPORT_URL = "https://mfr.moh.gov.et/api/Facility/ExportXLSX"
DEFAULT_ADM3 = Path("data/admin_boundaries/woredas_fcvmodified_adm3.geojson")
DEFAULT_WORKBOOK = Path("data/health_facilities/source/mfr_health_facilities.xlsx")
DEFAULT_RAW = Path("data/health_facilities/mfr_health_facilities_raw.json")
DEFAULT_OUTPUT = Path("data/health_facilities/mfr_health_facilities_coordinates.geojson")
DEFAULT_METADATA = Path("data/health_facilities/mfr_health_facilities_metadata.json")
USER_AGENT = "ethiopia-geo-hub-mfr-health-facilities/1.0"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def as_float(value: object) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def download_workbook(url: str, output: Path, timeout: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with requests.post(
        url,
        json={},
        headers={"Accept": "*/*", "Content-Type": "application/json", "User-Agent": USER_AGENT},
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


def cell_value(cell: Any) -> str:
    value = cell.find(f"{NS}v")
    if value is None or value.text is None:
        return ""
    return value.text


def read_mfr_xlsx(path: Path) -> list[dict[str, str]]:
    """Read the MFR XLSX without openpyxl because the export has invalid styles."""
    with zipfile.ZipFile(path) as archive:
        with archive.open("xl/worksheets/sheet1.xml") as sheet:
            rows: list[dict[str, str]] = []
            headers: list[str] | None = None
            row_number = 0
            for _event, row in iterparse(sheet, events=("end",)):
                if row.tag != f"{NS}row":
                    continue
                row_number += 1
                values = [cell_value(cell) for cell in row.findall(f"{NS}c")]
                row.clear()
                if row_number == 1:
                    continue
                if row_number == 2:
                    headers = [clean(value) for value in values]
                    continue
                if headers is None or not any(clean(value) for value in values):
                    continue
                if len(values) < len(headers):
                    values.extend([""] * (len(headers) - len(values)))
                rows.append({header: values[index] if index < len(values) else "" for index, header in enumerate(headers)})
    return rows


def facility_properties(row: dict[str, str]) -> dict[str, Any]:
    return {
        "facility_id": clean(row.get("Id")),
        "facility_name": clean(row.get("Name")),
        "active": clean(row.get("Active")),
        "settlement": clean(row.get("Settlement")),
        "latitude": as_float(row.get("Latitude")),
        "longitude": as_float(row.get("Longitude")),
        "altitude": as_float(row.get("Altitude")),
        "source_region": clean(row.get("Region")),
        "source_zone": clean(row.get("Zone")),
        "source_woreda": clean(row.get("Woreda")),
        "ownership": clean(row.get("Ownership")),
        "facility_type": clean(row.get("Type")),
        "facility_subtype": clean(row.get("SubType")),
        "status": clean(row.get("Status")),
        "reports_to": clean(row.get("ReportsTo")),
        "operational_status": clean(row.get("OperationalStatus")),
        "construction_status": clean(row.get("ConstructionStatus")),
        "dhis2_id": clean(row.get("Dhis2Id")),
        "national_facility_id": clean(row.get("EthiopianNationalFacilityId")),
        "geometry_source": "mfr_export_longitude_latitude",
        "source": "Ethiopia Ministry of Health Master Facility Registry",
    }


def in_ethiopia_bbox(lon: float | None, lat: float | None) -> bool:
    return lon is not None and lat is not None and 32.0 <= lon <= 49.0 and 3.0 <= lat <= 16.0


def build_geojson(
    rows: list[dict[str, str]],
    adm3_path: Path,
    workbook_path: Path,
    output_path: Path,
    metadata_path: Path,
    raw_path: Path,
) -> dict[str, Any]:
    props = [facility_properties(row) for row in rows]
    with_coords = [item for item in props if in_ethiopia_bbox(item["longitude"], item["latitude"])]
    invalid_coord_count = len(props) - len(with_coords)

    points = gpd.GeoDataFrame(
        with_coords,
        geometry=gpd.points_from_xy([item["longitude"] for item in with_coords], [item["latitude"] for item in with_coords]),
        crs="EPSG:4326",
    )
    adm3 = gpd.read_file(adm3_path)[
        ["MergeTEXT", "admin3Name", "admin3RefN", "admin3AltN", "admin2Name", "admin2Pcod", "admin1Name", "admin1Pcod", "geometry"]
    ].rename(
        columns={
            "MergeTEXT": "adm3_pcode",
            "admin3Name": "adm3_name",
            "admin3RefN": "adm3_ref_name",
            "admin3AltN": "adm3_alt_name",
            "admin2Name": "adm2_name",
            "admin2Pcod": "adm2_pcode",
            "admin1Name": "adm1_name",
            "admin1Pcod": "adm1_pcode",
        }
    )

    joined = gpd.sjoin(points, adm3, how="left", predicate="intersects")
    if "index_right" in joined.columns:
        joined = joined.sort_values(["facility_id", "index_right"], na_position="last").drop_duplicates("facility_id", keep="first")
        joined = joined.drop(columns=["index_right"])
    joined["admin_join_status"] = joined["adm3_pcode"].apply(lambda value: "matched" if clean(value) else "outside_or_unmatched")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    joined.to_file(output_path, driver="GeoJSON")

    write_json(raw_path, {"model": rows, "downloaded_at_utc": datetime.now(timezone.utc).isoformat()})

    matched = int((joined["admin_join_status"] == "matched").sum())
    unmatched = int((joined["admin_join_status"] != "matched").sum())
    source_region_counts = Counter(clean(item["source_region"]) or "(blank)" for item in joined.to_dict("records"))
    unmatched_by_source_region = Counter(
        clean(item["source_region"]) or "(blank)"
        for item in joined[joined["admin_join_status"] != "matched"].to_dict("records")
    )
    unmatched_examples = []
    for item in joined[joined["admin_join_status"] != "matched"].head(200).to_dict("records"):
        unmatched_examples.append(
            {
                "facility_id": clean(item.get("facility_id")),
                "facility_name": clean(item.get("facility_name")),
                "longitude": item.get("longitude"),
                "latitude": item.get("latitude"),
                "source_region": clean(item.get("source_region")),
                "source_zone": clean(item.get("source_zone")),
                "source_woreda": clean(item.get("source_woreda")),
            }
        )

    metadata = {
        "dataset": "Ethiopia Ministry of Health Master Facility Registry",
        "source_url": "https://mfrv2.moh.gov.et/#/facility/all",
        "api_url": DEFAULT_EXPORT_URL,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "workbook": str(workbook_path),
        "raw": str(raw_path),
        "boundary": str(adm3_path),
        "output": str(output_path),
        "source_facility_count": len(rows),
        "coordinate_facility_count": len(with_coords),
        "missing_or_invalid_coordinate_count": invalid_coord_count,
        "admin_matched_coordinate_count": matched,
        "admin_unmatched_coordinate_count": unmatched,
        "admin_matched_coordinate_percent": round((matched / len(with_coords)) * 100, 2) if with_coords else 0,
        "boundary_adm3_count": len(adm3),
        "matched_adm3_count": int(joined["adm3_pcode"].nunique(dropna=True)),
        "source_region_counts": dict(sorted(source_region_counts.items())),
        "admin_unmatched_by_source_region": dict(sorted(unmatched_by_source_region.items())),
        "admin_unmatched_examples": unmatched_examples,
    }
    write_json(metadata_path, metadata)
    return metadata


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
        temp_path = Path(file.name)
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    temp_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-url", default=DEFAULT_EXPORT_URL)
    parser.add_argument("--adm3", type=Path, default=DEFAULT_ADM3)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.skip_download:
        download_workbook(args.export_url, args.workbook, args.timeout)
    rows = read_mfr_xlsx(args.workbook)
    metadata = build_geojson(rows, args.adm3, args.workbook, args.output, args.metadata, args.raw)
    print(f"Wrote {args.workbook}")
    print(f"Wrote {args.raw} with {metadata['source_facility_count']:,} source facilities")
    print(f"Wrote {args.output} with {metadata['coordinate_facility_count']:,} coordinate facilities")
    print(f"Wrote {args.metadata}")
    print(
        "Merge summary: "
        f"coordinate facilities={metadata['coordinate_facility_count']:,}, "
        f"admin matched={metadata['admin_matched_coordinate_count']:,} "
        f"({metadata['admin_matched_coordinate_percent']}%), "
        f"admin unmatched={metadata['admin_unmatched_coordinate_count']:,}, "
        f"missing/invalid coordinates={metadata['missing_or_invalid_coordinate_count']:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
