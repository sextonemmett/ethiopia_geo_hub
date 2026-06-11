#!/usr/bin/env python3
"""Build project ADM3 polygons from the PIU survey export.

The PIU survey stores selected woredas as expanded checkbox columns. This script
explodes those columns, matches the selected woreda names to the FCV-modified
ADM3 shapefile, and writes one GeoJSON feature per project x ADM3 polygon.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import shapefile


DEFAULT_SURVEY = Path(
    "data/test_piu_results/PIU Survey Results - as of 2026.25.05(Mapping WB-Supported Portfol...csv"
)
DEFAULT_ADM3 = Path("data/admin_boundaries/Woredas_FCVModified/Eth_Admin3_v2.shp")
DEFAULT_ALIASES = Path("data/admin_boundaries/piu_adm3_aliases.csv")
DEFAULT_OUTPUT = Path("data/processed/piu_project_locations_adm3.geojson")
DEFAULT_REPORT = Path("data/processed/piu_project_locations_adm3_match_report.csv")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def truthy(value: object) -> bool:
    value = clean(value).lower()
    return value not in {"", "0", "false", "no", "nan", "none"}


def norm(value: object) -> str:
    value = clean(value).lower()
    replacements = {
        "addis abeba": "addis ababa",
        "gumuz": "gumz",
        "gojjam": "gojam",
        "wellega": "welega",
        "hararghe": "hararge",
        "haraghe": "hararge",
        "gurage": "guraghe",
        "hawassa": "hawasa",
        "wello": "welo",
        "kellem": "kelem",
        "southwest": "south west",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"\([^)]*\)", "", value)
    for word in ["region", "special woreda", "woreda", "town", "city", "administration"]:
        value = value.replace(word, "")
    value = value.replace("'", "").replace("’", "")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def split_project(value: str) -> tuple[str, str]:
    match = re.match(r"^(P\d+)\s+(.*)$", clean(value))
    if not match:
        return "", clean(value)
    return match.group(1), match.group(2)


def find_col(headers: list[str], prefix: str) -> int:
    for index, header in enumerate(headers):
        if header.startswith(prefix):
            return index
    raise KeyError(f"Column not found with prefix: {prefix}")


def is_adm3_checkbox_column(left_header: str) -> bool:
    lowered = left_header.lower()
    return (
        "woreda" in lowered
        and "zone" not in lowered
        and ("if you selected" in lowered or lowered.startswith("in "))
    )


def selected_parent(left_header: str) -> str:
    left_header = clean(left_header)
    match = re.match(r"If you selected (.*?), please indicate", left_header, re.I)
    if match:
        return clean(match.group(1))
    match = re.match(r"In (.*?)(?: Region|,| please)", left_header, re.I)
    if match:
        return clean(match.group(1))
    return ""


def load_aliases(path: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if not path.exists():
        return aliases
    with path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            survey_name = clean(row.get("survey_name"))
            boundary_name = clean(row.get("boundary_name"))
            if survey_name and boundary_name:
                aliases[norm(survey_name)] = norm(boundary_name)
    return aliases


def load_survey(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="cp1252") as file:
        rows = list(csv.reader(file))
    return [clean(value) for value in rows[0]], [[clean(value) for value in row] for row in rows[1:]]


def load_adm3(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    reader = shapefile.Reader(str(path), encoding="utf-8")
    features: list[dict] = []
    by_name: dict[str, list[dict]] = defaultdict(list)

    for shape_record in reader.iterShapeRecords():
        record = shape_record.record.as_dict()
        props = {key: clean(value) for key, value in record.items()}
        feature = {
            "geometry": shape_record.shape.__geo_interface__,
            "properties": props,
        }
        features.append(feature)
        for field in ["admin3Name", "admin3RefN", "admin3AltN"]:
            value = props.get(field)
            if value:
                by_name[norm(value)].append(feature)

    return features, by_name


def choose_match(
    survey_woreda: str,
    parent_name: str,
    by_name: dict[str, list[dict]],
    aliases: dict[str, str],
) -> tuple[dict | None, str, str]:
    source_norm = norm(survey_woreda)
    target_norm = aliases.get(source_norm, source_norm)
    candidates = by_name.get(target_norm, [])
    if not candidates:
        return None, "unmatched", ""

    parent_norm = norm(parent_name)
    if len(candidates) > 1 and parent_norm:
        parent_filtered = [
            candidate
            for candidate in candidates
            if parent_norm in {
                norm(candidate["properties"].get("admin2Name")),
                norm(candidate["properties"].get("admin1Name")),
            }
        ]
        if parent_filtered:
            candidates = parent_filtered

    exact_candidates = [
        candidate
        for candidate in candidates
        if clean(survey_woreda).lower() == clean(candidate["properties"].get("admin3Name")).lower()
    ]
    if exact_candidates:
        candidates = exact_candidates

    if len(candidates) > 1 and "town" not in clean(survey_woreda).lower():
        non_town_candidates = [
            candidate
            for candidate in candidates
            if "town" not in clean(candidate["properties"].get("admin3Name")).lower()
        ]
        if non_town_candidates:
            candidates = non_town_candidates

    method = "alias" if target_norm != source_norm else "normalized"
    if clean(survey_woreda) == candidates[0]["properties"].get("admin3Name"):
        method = "exact"
    if len(candidates) > 1:
        return candidates[0], f"{method}_ambiguous_first", candidates[0]["properties"].get("admin3Name", "")
    return candidates[0], method, candidates[0]["properties"].get("admin3Name", "")


def add_value(bucket: set[str], value: str) -> None:
    value = clean(value)
    if value:
        bucket.add(value)


def build_locations(
    survey_path: Path,
    adm3_path: Path,
    alias_path: Path,
) -> tuple[dict, list[dict], dict]:
    headers, rows = load_survey(survey_path)
    _, by_name = load_adm3(adm3_path)
    aliases = load_aliases(alias_path)

    col_project = find_col(headers, "Q1. Please indicate")
    col_capacity = find_col(headers, "Q3. In what capacity")
    col_instrument = find_col(headers, "Q4. What is/are the lending")
    col_lead = find_col(headers, "Q5. Which Federal Ministry")
    col_regions = find_col(headers, "Q7. Please indicate")
    col_comments = find_col(headers, "Q9. Final question")
    col_uuid = headers.index("_uuid") if "_uuid" in headers else None
    col_submission_time = headers.index("_submission_time") if "_submission_time" in headers else None
    rank_cols = [index for index, header in enumerate(headers) if header in {"1st choice", "2nd choice", "3rd choice", "4th choice"}]

    adm3_columns = []
    for index, header in enumerate(headers):
        if "/" not in header:
            continue
        left, survey_woreda = header.rsplit("/", 1)
        if is_adm3_checkbox_column(left):
            adm3_columns.append((index, clean(survey_woreda), selected_parent(left)))

    grouped: dict[tuple[str, str], dict] = {}
    match_report: list[dict] = []
    stats = {
        "survey_rows": len(rows),
        "adm3_checkbox_columns": len(adm3_columns),
        "selected_adm3_cells": 0,
        "matched_selected_adm3_cells": 0,
        "unmatched_selected_adm3_cells": 0,
    }

    for row_number, row in enumerate(rows, start=2):
        project_code, project_name = split_project(row[col_project])
        submission_uuid = row[col_uuid] if col_uuid is not None else ""
        submission_time = row[col_submission_time] if col_submission_time is not None else ""
        challenges = [row[index] for index in rank_cols if truthy(row[index])]

        for column_index, survey_woreda, parent_name in adm3_columns:
            if not truthy(row[column_index]):
                continue

            stats["selected_adm3_cells"] += 1
            match, match_method, boundary_name = choose_match(survey_woreda, parent_name, by_name, aliases)
            report_row = {
                "csv_row": row_number,
                "project_code": project_code,
                "project_name": project_name,
                "survey_woreda": survey_woreda,
                "survey_parent": parent_name,
                "match_status": "matched" if match else "unmatched",
                "match_method": match_method,
                "boundary_woreda": boundary_name,
                "boundary_zone": match["properties"].get("admin2Name", "") if match else "",
                "boundary_region": match["properties"].get("admin1Name", "") if match else "",
                "boundary_pcode": match["properties"].get("MergeTEXT", "") if match else "",
            }
            match_report.append(report_row)

            if not match:
                stats["unmatched_selected_adm3_cells"] += 1
                continue

            stats["matched_selected_adm3_cells"] += 1
            boundary_props = match["properties"]
            key = (project_code or project_name, boundary_props["MergeTEXT"])
            if key not in grouped:
                grouped[key] = {
                    "type": "Feature",
                    "geometry": match["geometry"],
                    "properties": {
                        "project_code": project_code,
                        "project_name": project_name,
                        "admin_level": 3,
                        "adm3_pcode": boundary_props["MergeTEXT"],
                        "adm3_name": boundary_props["admin3Name"],
                        "adm2_name": boundary_props["admin2Name"],
                        "adm2_pcode": boundary_props["admin2Pcod"],
                        "adm1_name": boundary_props["admin1Name"],
                        "adm1_pcode": boundary_props["admin1Pcod"],
                        "survey_selection_count": 0,
                        "survey_woreda_names": set(),
                        "lending_instruments": set(),
                        "lead_implementers": set(),
                        "respondent_roles": set(),
                        "survey_regions": set(),
                        "challenge_rankings": set(),
                        "submission_uuids": set(),
                        "submission_times": set(),
                        "has_comment": False,
                    },
                }

            props = grouped[key]["properties"]
            props["survey_selection_count"] += 1
            add_value(props["survey_woreda_names"], survey_woreda)
            add_value(props["lending_instruments"], row[col_instrument])
            add_value(props["lead_implementers"], row[col_lead])
            add_value(props["respondent_roles"], row[col_capacity])
            add_value(props["survey_regions"], row[col_regions])
            add_value(props["submission_uuids"], submission_uuid)
            add_value(props["submission_times"], submission_time)
            for challenge in challenges:
                add_value(props["challenge_rankings"], challenge)
            if truthy(row[col_comments]):
                props["has_comment"] = True

    for feature in grouped.values():
        props = feature["properties"]
        for key, value in list(props.items()):
            if isinstance(value, set):
                props[key] = "; ".join(sorted(value))

    collection = {
        "type": "FeatureCollection",
        "name": "piu_project_locations_adm3",
        "features": list(grouped.values()),
    }
    stats["output_features"] = len(collection["features"])
    return collection, match_report, stats


def write_report(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "csv_row",
        "project_code",
        "project_name",
        "survey_woreda",
        "survey_parent",
        "match_status",
        "match_method",
        "boundary_woreda",
        "boundary_zone",
        "boundary_region",
        "boundary_pcode",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", type=Path, default=DEFAULT_SURVEY)
    parser.add_argument("--adm3", type=Path, default=DEFAULT_ADM3)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    collection, match_report, stats = build_locations(args.survey, args.adm3, args.aliases)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    write_report(args.report, match_report)

    print("PIU ADM3 project locations built.")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  output: {args.output}")
    print(f"  report: {args.report}")


if __name__ == "__main__":
    main()
