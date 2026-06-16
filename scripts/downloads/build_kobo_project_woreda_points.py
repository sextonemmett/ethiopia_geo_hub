#!/usr/bin/env python3
"""Build an AGOL-ready Kobo project/woreda centroid point layer.

The output grain is one feature per project x matched ADM3 woreda. Repeated
submissions for the same project and woreda are aggregated into one point with
selection and submission counts.

Kobo choice labels are read from an asset metadata JSON file or fetched from the
Kobo API when KOBO_API_TOKEN is set.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import shapefile


DEFAULT_ASSET_ID = "aKX2WQUSmQ87QrCUxJzWCp"
DEFAULT_BASE_URL = "https://kf.kobotoolbox.org/api/v2"
DEFAULT_KOBO_DATA = Path("data/projects/kobo_project_data.json")
DEFAULT_ADM3 = Path("data/admin_boundaries/Woredas_FCVModified/Eth_Admin3_v2.shp")
DEFAULT_ALIASES = Path("data/admin_boundaries/piu_adm3_aliases.csv")
DEFAULT_OUTPUT = Path("data/projects/kobo_project_woreda_points_agol.geojson")

PROJECT_PATH = "group_zf0xx07/Q1_Please_indicate_which_proj"
ROLE_PATH = "group_zf0xx07/Q2_In_what_capacity_you_are_f"
RESPONDENT_PATH = "group_zf0xx07/Q2_What_is_your_name"
INSTRUMENT_PATH = "group_zf0xx07/Q4_What_is_are_the_s_for_this_project"
LEAD_PATH = "group_zf0xx07/Q5_Which_Federal_Ministry_or_"
OTHER_LEAD_PATH = "group_zf0xx07/If_you_answered_oth_s_project_or_program"
OTHER_IMPLEMENTERS_PATH = "group_zf0xx07/Q6_Which_other_mini_the_lead_implementer"
REGION_PATH = "group_tz8yn95/Which_region_s_is_this_projec"
COMMENT_PATH = "group_hx5kb98/Q9_Final_question_our_survey_responses"
RANK_PATHS = [
    "group_hx5kb98/Q8_What_are_the_top_faced_by_the_project/_1st_choice",
    "group_hx5kb98/Q8_What_are_the_top_faced_by_the_project/_2nd_choice",
    "group_hx5kb98/Q8_What_are_the_top_faced_by_the_project/_3rd_choice",
    "group_hx5kb98/Q8_What_are_the_top_faced_by_the_project/_4th_choice",
]

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


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def label_text(value: object) -> str:
    if isinstance(value, list):
        return clean(value[0] if value else "")
    return clean(value)


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
        "sidama region": "sidama",
        "zone 1 awsi": "awsi",
        "zone 2 kilbati": "kilbati",
        "zone 3 gabi": "gabi",
        "zone 4 fanti": "fanti",
        "zone 5 hari": "hari",
        "zone 6 mahi": "mahi",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"\([^)]*\)", "", value)
    for word in ["region", "special woreda", "woreda", "town", "city", "administration", "special zone", "zone"]:
        value = value.replace(word, "")
    value = value.replace("'", "").replace("’", "")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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


def load_asset_metadata(path: Path | None, base_url: str, asset_id: str) -> dict[str, Any]:
    if path:
        return json.loads(path.read_text(encoding="utf-8"))
    token = os.getenv("KOBO_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Provide --asset-metadata or set KOBO_API_TOKEN so form choices can be loaded.")
    url = f"{base_url.rstrip('/')}/assets/{asset_id}/"
    request = Request(url, headers={"Accept": "application/json", "Authorization": f"Token {token}"}, method="GET")
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"Kobo asset metadata request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Kobo asset metadata request failed: {exc.reason}") from exc


def split_project(value: str) -> tuple[str, str]:
    value = clean(value)
    match = re.match(r"^\s*(P\d+)\s+(.*)$", value, re.I)
    if match:
        return match.group(1).upper(), clean(match.group(2))
    code_match = re.search(r"p\d{6}", value, re.I)
    return (code_match.group(0).upper() if code_match else "", value)


def parent_from_label(label: str) -> str:
    match = re.search(r"If you selected (.*?), please indicate", label, re.I)
    if match:
        return clean(match.group(1))
    match = re.search(r"In (.*?)(?: Region|,| please)", label, re.I)
    if match:
        return clean(match.group(1))
    return ""


def join_limited(values: set[str] | list[str], limit: int = 3900) -> str:
    ordered = sorted({clean(value) for value in values if clean(value)})
    output: list[str] = []
    used = 0
    for index, value in enumerate(ordered):
        sep = "; " if output else ""
        remaining = len(ordered) - index
        suffix = f"; ... (+{remaining} more)" if remaining else ""
        if used + len(sep) + len(value) + len(suffix) > limit:
            output.append(f"{sep}... (+{remaining} more)")
            break
        output.append(f"{sep}{value}")
        used += len(sep) + len(value)
    return "".join(output)


def iter_points(coords: Any):
    if not coords:
        return
    first = coords[0]
    if isinstance(first, (int, float)) and len(coords) >= 2:
        yield float(coords[0]), float(coords[1])
    else:
        for part in coords:
            yield from iter_points(part)


def ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    area = 0.0
    cx = 0.0
    cy = 0.0
    if len(ring) < 3:
        return area, cx, cy
    for index in range(len(ring) - 1):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index + 1][0], ring[index + 1][1]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area *= 0.5
    if area == 0:
        return area, cx, cy
    return area, cx / (6 * area), cy / (6 * area)


def polygon_centroid(rings: list[list[list[float]]]) -> tuple[float, float, float]:
    total_area = 0.0
    total_x = 0.0
    total_y = 0.0
    for ring in rings:
        area, cx, cy = ring_centroid(ring)
        if area:
            total_area += area
            total_x += cx * area
            total_y += cy * area
    if total_area:
        return abs(total_area), total_x / total_area, total_y / total_area
    return 0.0, 0.0, 0.0


def geometry_centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polygons = [coords] if geom_type == "Polygon" else coords if geom_type == "MultiPolygon" else []
    total_area = 0.0
    total_x = 0.0
    total_y = 0.0
    for polygon in polygons:
        area, x, y = polygon_centroid(polygon)
        if area:
            total_area += area
            total_x += x * area
            total_y += y * area
    if total_area:
        return total_x / total_area, total_y / total_area
    points = list(iter_points(coords))
    if not points:
        raise ValueError("Cannot compute centroid for empty geometry")
    return sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)


def load_adm3(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    reader = shapefile.Reader(str(path), encoding="utf-8")
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_key: dict[str, dict[str, Any]] = {}
    for index, shape_record in enumerate(reader.iterShapeRecords()):
        props = {key: clean(value) for key, value in shape_record.record.as_dict().items()}
        if not props.get("admin3Name"):
            continue
        geometry = shape_record.shape.__geo_interface__
        lon, lat = geometry_centroid(geometry)
        key = props.get("MergeTEXT") or f"adm3_{index}"
        feature = {"key": key, "geometry": geometry, "centroid": [lon, lat], "properties": props}
        by_key[key] = feature
        for field in ["admin3Name", "admin3RefN", "admin3AltN"]:
            value = props.get(field)
            if value:
                by_name[norm(value)].append(feature)
    return by_name, by_key


def choose_match(
    woreda_label: str,
    parent: str,
    regions: list[str],
    by_name: dict[str, list[dict[str, Any]]],
    aliases: dict[str, str],
) -> tuple[dict[str, Any] | None, str]:
    source = norm(woreda_label)
    target = aliases.get(source, source)
    candidates = list(by_name.get(target, []))
    if not candidates:
        return None, "unmatched"
    region_norms = {norm(region) for region in regions if region}
    if len(candidates) > 1 and region_norms:
        filtered = [candidate for candidate in candidates if norm(candidate["properties"].get("admin1Name")) in region_norms]
        if filtered:
            candidates = filtered
    parent_norm = norm(parent)
    if len(candidates) > 1 and parent_norm:
        filtered = []
        for candidate in candidates:
            values = {
                norm(candidate["properties"].get("admin2Name")),
                norm(candidate["properties"].get("admin1Name")),
            }
            if parent_norm in values or any(parent_norm and (parent_norm in value or value in parent_norm) for value in values):
                filtered.append(candidate)
        if filtered:
            candidates = filtered
    if len(candidates) > 1 and "town" not in clean(woreda_label).lower():
        filtered = [candidate for candidate in candidates if "town" not in clean(candidate["properties"].get("admin3Name")).lower()]
        if filtered:
            candidates = filtered
    method = "alias" if target != source else "normalized"
    if len(candidates) > 1:
        method += "_ambiguous_first"
    return candidates[0], method


def build_choice_helpers(asset: dict[str, Any]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]], list[dict[str, str]]]:
    choices: dict[str, dict[str, str]] = defaultdict(dict)
    for choice in asset["content"]["choices"]:
        choices[choice.get("list_name")][choice.get("name")] = label_text(choice.get("label"))
    survey_by_path: dict[str, dict[str, Any]] = {}
    woreda_fields: list[dict[str, str]] = []
    for item in asset["content"]["survey"]:
        path = item.get("$xpath")
        if path:
            survey_by_path[path] = item
        label = label_text(item.get("label"))
        if path and item.get("type") == "select_multiple" and item.get("select_from_list_name") and "woreda(s)" in label.lower():
            woreda_fields.append(
                {
                    "path": path,
                    "list_name": item["select_from_list_name"],
                    "label": label,
                    "parent": parent_from_label(label),
                }
            )
    return choices, survey_by_path, woreda_fields


def translate(path: str, value: object, choices: dict[str, dict[str, str]], survey_by_path: dict[str, dict[str, Any]]) -> str:
    value = clean(value)
    if not value:
        return ""
    list_name = survey_by_path.get(path, {}).get("select_from_list_name")
    labels = [choices.get(list_name, {}).get(part, part) for part in value.split()]
    return "; ".join(clean(label) for label in labels if clean(label))


def new_bucket(project_code: str, project_name: str, project_label: str, adm3: dict[str, Any], survey_woreda: str, parent: str) -> dict[str, Any]:
    props = adm3["properties"]
    return {
        "project_code": project_code,
        "project_name": project_name,
        "project_label": project_label,
        "admin_level": 3,
        "adm3_key": adm3["key"],
        "adm3_name": props.get("admin3Name", ""),
        "adm3_ref_name": props.get("admin3RefN", ""),
        "adm3_alt_name": props.get("admin3AltN", ""),
        "adm2_name": props.get("admin2Name", ""),
        "adm2_pcode": props.get("admin2Pcod", ""),
        "adm1_name": props.get("admin1Name", ""),
        "adm1_pcode": props.get("admin1Pcod", ""),
        "survey_woredas": {survey_woreda},
        "survey_parents": {parent},
        "survey_regions": set(),
        "submission_uuids": set(),
        "submission_times": set(),
        "respondent_names": set(),
        "respondent_roles": set(),
        "lending_instruments": set(),
        "lead_implementers": set(),
        "other_lead_implementers": set(),
        "other_implementers": set(),
        "challenge_rankings": set(),
        "comment_count": 0,
        "selection_count": 0,
        "match_methods": Counter(),
        "centroid": adm3["centroid"],
    }


def add_submission_context(bucket: dict[str, Any], row: dict[str, Any], choices: dict[str, dict[str, str]], survey_by_path: dict[str, dict[str, Any]]) -> None:
    if clean(row.get("_uuid")):
        bucket["submission_uuids"].add(clean(row.get("_uuid")))
    if clean(row.get("_submission_time")):
        bucket["submission_times"].add(clean(row.get("_submission_time"))[:10])
    if clean(row.get(RESPONDENT_PATH)):
        bucket["respondent_names"].add(clean(row.get(RESPONDENT_PATH)))
    for path, target in [
        (ROLE_PATH, "respondent_roles"),
        (INSTRUMENT_PATH, "lending_instruments"),
        (LEAD_PATH, "lead_implementers"),
        (OTHER_IMPLEMENTERS_PATH, "other_implementers"),
        (REGION_PATH, "survey_regions"),
    ]:
        value = translate(path, row.get(path), choices, survey_by_path)
        if value:
            bucket[target].update(part.strip() for part in value.split(";") if part.strip())
    if clean(row.get(OTHER_LEAD_PATH)):
        bucket["other_lead_implementers"].add(clean(row.get(OTHER_LEAD_PATH)))
    ranks = [translate(path, row.get(path), choices, survey_by_path) for path in RANK_PATHS if clean(row.get(path))]
    if ranks:
        bucket["challenge_rankings"].add("; ".join(ranks))
    if clean(row.get(COMMENT_PATH)):
        bucket["comment_count"] += 1


def build_layer(args: argparse.Namespace) -> dict[str, Any]:
    asset = load_asset_metadata(args.asset_metadata, args.base_url, args.asset_id)
    choices, survey_by_path, woreda_fields = build_choice_helpers(asset)
    aliases = load_aliases(args.aliases)
    by_name, _ = load_adm3(args.adm3)
    rows = json.loads(args.kobo_data.read_text(encoding="utf-8")).get("results") or []
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    unmatched = 0

    for row in rows:
        project_label = translate(PROJECT_PATH, row.get(PROJECT_PATH), choices, survey_by_path) or clean(row.get(PROJECT_PATH))
        project_code, project_name = split_project(project_label)
        if not project_code:
            project_code = clean(row.get(PROJECT_PATH))
        row_regions = translate(REGION_PATH, row.get(REGION_PATH), choices, survey_by_path)
        regions = [part.strip() for part in row_regions.split(";") if part.strip()]

        for field in woreda_fields:
            raw = clean(row.get(field["path"]))
            if not raw:
                continue
            for code in raw.split():
                survey_woreda = choices.get(field["list_name"], {}).get(code, code)
                match, method = choose_match(survey_woreda, field["parent"], regions, by_name, aliases)
                if not match:
                    unmatched += 1
                    continue
                key = (project_code, match["key"])
                if key not in buckets:
                    buckets[key] = new_bucket(project_code, project_name, project_label, match, survey_woreda, field["parent"])
                bucket = buckets[key]
                bucket["survey_woredas"].add(survey_woreda)
                bucket["survey_parents"].add(field["parent"])
                bucket["selection_count"] += 1
                bucket["match_methods"][method] += 1
                add_submission_context(bucket, row, choices, survey_by_path)

    features = []
    for bucket in sorted(buckets.values(), key=lambda item: (item["project_code"], item["adm1_name"], item["adm2_name"], item["adm3_name"])):
        lon, lat = bucket.pop("centroid")
        match_methods = bucket.pop("match_methods")
        properties = {
            **bucket,
            "submission_count": len(bucket["submission_uuids"]),
            "survey_woredas": join_limited(bucket["survey_woredas"]),
            "survey_parents": join_limited(bucket["survey_parents"]),
            "survey_regions": join_limited(bucket["survey_regions"]),
            "submission_uuids": join_limited(bucket["submission_uuids"]),
            "submission_times": join_limited(bucket["submission_times"]),
            "respondent_names": join_limited(bucket["respondent_names"]),
            "respondent_roles": join_limited(bucket["respondent_roles"]),
            "lending_instruments": join_limited(bucket["lending_instruments"]),
            "lead_implementers": join_limited(bucket["lead_implementers"]),
            "other_lead_implementers": join_limited(bucket["other_lead_implementers"]),
            "other_implementers": join_limited(bucket["other_implementers"]),
            "challenge_rankings": join_limited(bucket["challenge_rankings"]),
            "match_methods": "; ".join(f"{key}:{value}" for key, value in sorted(match_methods.items())),
            "source": "KoboToolbox API / FCV-modified ADM3 boundaries",
        }
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": properties})

    print(f"Matched features={len(features):,}; unmatched selections={unmatched:,}")
    return {"type": "FeatureCollection", "features": features}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kobo-data", type=Path, default=DEFAULT_KOBO_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--adm3", type=Path, default=DEFAULT_ADM3)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--asset-metadata", type=Path, default=None)
    parser.add_argument("--asset-id", default=DEFAULT_ASSET_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    payload = build_layer(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.output} with {len(payload['features']):,} point features")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
