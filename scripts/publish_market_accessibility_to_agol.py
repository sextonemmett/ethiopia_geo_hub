#!/usr/bin/env python3
"""Publish Ethiopia market accessibility outputs to ArcGIS Online.

Publishes the six main national 500 m outputs:
  - two accessibility GeoTIFFs as hosted imagery layers
  - two isochrone GeoJSONs as hosted feature services
  - two allocated-market GeoJSONs as hosted feature services

Authentication uses the browser OAuth flow from publish_geojson_to_agol.py.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any
import warnings

from arcgis.gis import GIS
from arcgis.raster.analytics import copy_raster
import geopandas as gpd
import rasterio
from shapely import box, make_valid
from shapely.geometry import MultiPolygon, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_geojson_to_agol import (  # noqa: E402
    agol_request,
    find_feature_service_by_title,
    get_agol_browser_token,
    get_item,
    load_dotenv,
    load_geojson,
    prepare_geojson_features,
    publish_layer,
    validate_agol_user,
)
from publish_worldpop_rasters_to_agol import (  # noqa: E402
    ETHIOPIA_CORE_TEAM_GROUP_ID,
    delete_item,
    service_name,
    share_item as share_raster_item,
    update_item_metadata as update_raster_item_metadata,
)


DEFAULT_METADATA = Path("data/market_accessibility/ethiopia_market_accessibility_500m_metadata.json")
DEFAULT_PORTAL = "https://www.arcgis.com"
DEFAULT_TAGS = ["ethiopia", "geo hub", "eth", "market access", "accessibility", "500m"]
ISOCHRONE_AGOL_DICE_CELL_DEGREES = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--group-id", action="append", default=[ETHIOPIA_CORE_TEAM_GROUP_ID])
    parser.add_argument("--delete-existing", action="store_true", help="Delete item IDs already recorded in metadata before republishing.")
    parser.add_argument("--delete-item-id", action="append", default=[], help="Additional obsolete AGOL item ID to delete.")
    parser.add_argument("--service-name-suffix", default="", help="Suffix to append to hosted imagery service names.")
    parser.add_argument(
        "--only",
        default="all",
        help="Comma-separated spec keys to publish, or all. Useful for resuming a partial run.",
    )
    return parser.parse_args()


def read_raster_profile(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as src:
        return {
            "crs": str(src.crs),
            "bounds": {
                "xmin": src.bounds.left,
                "ymin": src.bounds.bottom,
                "xmax": src.bounds.right,
                "ymax": src.bounds.top,
            },
            "width": src.width,
            "height": src.height,
            "pixel_size_x": abs(src.res[0]),
            "pixel_size_y": abs(src.res[1]),
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
        }


def item_summary(portal: str, token: str, item_id: str) -> dict[str, Any]:
    item = get_item(portal, token, item_id)
    return {"item_id": item_id, "title": item.get("title"), "type": item.get("type"), "url": item.get("url")}


def find_item_by_title(
    portal: str,
    token: str,
    username: str,
    title: str,
    item_type: str,
) -> dict[str, Any] | None:
    payload = agol_request(
        "GET",
        f"{portal}/sharing/rest/search",
        context=f"AGOL search failed for {title}",
        token=token,
        params={
            "f": "json",
            "q": f'title:"{title}" AND owner:{username} AND type:"{item_type}"',
            "num": 10,
        },
    )
    matches = [
        item
        for item in payload.get("results", [])
        if (item.get("title") or "").strip() == title and item.get("type") == item_type
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.get("modified") or 0, reverse=True)[0]


def raster_specs(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = metadata["outputs"]
    return [
        {
            "key": "accessibility_walk",
            "title": "Ethiopia Market Accessibility Walking Travel Time 500m",
            "path": outputs["walk"]["accessibility"],
            "snippet": "Walking-only travel time in minutes to the nearest Ethiopia market center at 500 m resolution.",
            "description": (
                "Continuous raster of modeled walking-only travel time, in minutes, to the nearest market center "
                "in Ethiopia. Built from HDX market centers, OVT roads, 2025 Esri/Impact Observatory land cover, "
                "elevation-derived slope, and WorldPop-aligned national analysis outputs. Pixel values are minutes."
            ),
            "tags": ["walking", "travel time", "market centers", "raster", "geotiff"],
        },
        {
            "key": "accessibility_least_cost",
            "title": "Ethiopia Market Accessibility Least-Cost Travel Time 500m",
            "path": outputs["least_cost"]["accessibility"],
            "snippet": "Least-cost travel time in minutes to the nearest Ethiopia market center at 500 m resolution.",
            "description": (
                "Continuous raster of modeled least-cost travel time, in minutes, to the nearest market center "
                "in Ethiopia. The least-cost mode uses motorized speeds on roads and walking speeds off-road. "
                "Built from HDX market centers, OVT roads, 2025 Esri/Impact Observatory land cover, and elevation-derived slope."
            ),
            "tags": ["least cost", "travel time", "market centers", "raster", "geotiff", "roads"],
        },
    ]


def feature_specs(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = metadata["outputs"]
    share = {"org": False, "everyone": False, "group_ids": [ETHIOPIA_CORE_TEAM_GROUP_ID]}
    walk_isochrone_path = agol_isochrone_copy_path(Path(outputs["walk"]["isochrones"]))
    least_isochrone_path = agol_isochrone_copy_path(Path(outputs["least_cost"]["isochrones"]))
    specs = [
        {
            "key": "isochrones_walk",
            "title": "Ethiopia Market Isochrones Walking 500m",
            "layer_name": "ethiopia_market_isochrones_walk_500m",
            "path": str(walk_isochrone_path),
            "snippet": "Walking-only market access isochrone bands for Ethiopia at 500 m resolution.",
            "description": (
                "Polygon isochrone bands derived from the 500 m walking-only market accessibility raster. "
                "Bands are 0-15, 15-30, 30-45, 45-60, 60-90, 90-120, 120-150, 150-180, and 180+ minutes. "
                "This hosted layer uses a diced AGOL publishing copy to avoid request-size limits; "
                "the unsplit analysis GeoJSON is retained locally."
            ),
            "license_info": "Derived analysis output. Source market centers are CC0 via HDX.",
            "share": share,
            "tags": ["isochrones", "walking", "market centers", "travel time", "polygons"],
            "batch_size": 25,
        },
    ]
    if outputs["walk"].get("isochrones_coarse"):
        specs.append(
            {
                "key": "isochrones_walk_coarse",
                "title": "Ethiopia Market Isochrones Walking Coarse Bands 500m",
                "layer_name": "ethiopia_market_isochrones_walk_coarse_500m",
                "path": str(agol_isochrone_copy_path(Path(outputs["walk"]["isochrones_coarse"]))),
                "snippet": "Walking-only market access isochrone bands for Ethiopia using five broad travel-time classes.",
                "description": (
                    "Polygon isochrone bands derived from the 500 m walking-only market accessibility raster. "
                    "Bands are 0-30 minutes, 30-60 minutes, 1-2 hours, 2-3 hours, and 3+ hours. "
                    "This hosted layer uses a diced AGOL publishing copy to avoid request-size limits; "
                    "the unsplit analysis GeoJSON is retained locally."
                ),
                "license_info": "Derived analysis output. Source market centers are CC0 via HDX.",
                "share": share,
                "tags": ["isochrones", "walking", "market centers", "travel time", "polygons", "coarse bands"],
                "batch_size": 25,
            }
        )
    specs.append(
        {
            "key": "isochrones_least_cost",
            "title": "Ethiopia Market Isochrones Least-Cost 500m",
            "layer_name": "ethiopia_market_isochrones_least_cost_500m",
            "path": str(least_isochrone_path),
            "snippet": "Least-cost market access isochrone bands for Ethiopia at 500 m resolution.",
            "description": (
                "Polygon isochrone bands derived from the 500 m least-cost market accessibility raster. "
                "The least-cost mode uses motorized speeds on roads and walking speeds off-road. "
                "This hosted layer uses a diced AGOL publishing copy to avoid request-size limits; "
                "the unsplit analysis GeoJSON is retained locally."
            ),
            "license_info": "Derived analysis output. Source market centers are CC0 via HDX.",
            "share": share,
            "tags": ["isochrones", "least cost", "market centers", "travel time", "polygons"],
            "batch_size": 25,
        }
    )
    if outputs["least_cost"].get("isochrones_coarse"):
        specs.append(
            {
                "key": "isochrones_least_cost_coarse",
                "title": "Ethiopia Market Isochrones Least-Cost Coarse Bands 500m",
                "layer_name": "ethiopia_market_isochrones_least_cost_coarse_500m",
                "path": str(agol_isochrone_copy_path(Path(outputs["least_cost"]["isochrones_coarse"]))),
                "snippet": "Least-cost market access isochrone bands for Ethiopia using five broad travel-time classes.",
                "description": (
                    "Polygon isochrone bands derived from the 500 m least-cost market accessibility raster. "
                    "Bands are 0-30 minutes, 30-60 minutes, 1-2 hours, 2-3 hours, and 3+ hours. "
                    "The least-cost mode uses motorized speeds on roads and walking speeds off-road. "
                    "This hosted layer uses a diced AGOL publishing copy to avoid request-size limits; "
                    "the unsplit analysis GeoJSON is retained locally."
                ),
                "license_info": "Derived analysis output. Source market centers are CC0 via HDX.",
                "share": share,
                "tags": ["isochrones", "least cost", "market centers", "travel time", "polygons", "coarse bands"],
                "batch_size": 25,
            }
        )
    specs.extend(
        [
        {
            "key": "allocated_markets_walk",
            "title": "Ethiopia Market Centers Allocated Population Walking 500m",
            "layer_name": "ethiopia_market_centers_allocated_population_walk_500m",
            "path": outputs["walk"]["allocated_markets"],
            "snippet": "Ethiopia market centers with allocated WorldPop population by nearest walking travel-time market.",
            "description": (
                "Market center point layer with allocated_pop, the WorldPop population for which each market is the "
                "nearest market by modeled walking-only travel time at 500 m resolution."
            ),
            "license_info": "Source market centers are Public Domain / CC0 via HDX. WorldPop source data terms apply.",
            "share": share,
            "tags": ["market centers", "allocated population", "walking", "worldpop", "points"],
            "batch_size": 1000,
        },
        {
            "key": "allocated_markets_least_cost",
            "title": "Ethiopia Market Centers Allocated Population Least-Cost 500m",
            "layer_name": "ethiopia_market_centers_allocated_population_least_cost_500m",
            "path": outputs["least_cost"]["allocated_markets"],
            "snippet": "Ethiopia market centers with allocated WorldPop population by nearest least-cost travel-time market.",
            "description": (
                "Market center point layer with allocated_pop, the WorldPop population for which each market is the "
                "nearest market by modeled least-cost travel time at 500 m resolution."
            ),
            "license_info": "Source market centers are Public Domain / CC0 via HDX. WorldPop source data terms apply.",
            "share": share,
            "tags": ["market centers", "allocated population", "least cost", "worldpop", "points"],
            "batch_size": 1000,
        },
        ]
    )
    return specs


def agol_isochrone_copy_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}_agol.geojson")


def polygonal_geometry(geom: Any) -> Polygon | MultiPolygon | None:
    geom = make_valid(geom)
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type == "MultiPolygon":
        return geom
    parts: list[Polygon] = []
    for part in getattr(geom, "geoms", []):
        polygonal = polygonal_geometry(part)
        if polygonal is None:
            continue
        if polygonal.geom_type == "Polygon":
            parts.append(polygonal)
        else:
            parts.extend(list(polygonal.geoms))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def prepare_agol_isochrone_copy(source_path: Path) -> dict[str, Any]:
    output_path = agol_isochrone_copy_path(source_path)
    gdf = gpd.read_file(source_path)
    original_vertices = int(gdf.geometry.count_coordinates().sum())

    minx, miny, maxx, maxy = gdf.total_bounds
    cell = ISOCHRONE_AGOL_DICE_CELL_DEGREES
    x0 = int(minx // cell) * cell
    y0 = int(miny // cell) * cell
    x_values = []
    x = x0
    while x < maxx:
        x_values.append(x)
        x += cell
    y_values = []
    y = y0
    while y < maxy:
        y_values.append(y)
        y += cell

    rows: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        props = row.drop(labels="geometry").to_dict()
        geom = polygonal_geometry(row.geometry)
        if geom is None or geom.is_empty:
            continue
        geom_minx, geom_miny, geom_maxx, geom_maxy = geom.bounds
        for gx in x_values:
            if gx > geom_maxx or gx + cell < geom_minx:
                continue
            for gy in y_values:
                if gy > geom_maxy or gy + cell < geom_miny:
                    continue
                tile = box(gx, gy, gx + cell, gy + cell)
                if not geom.intersects(tile):
                    continue
                clipped = polygonal_geometry(geom.intersection(tile))
                if clipped is None or clipped.is_empty:
                    continue
                rows.append({**props, "geometry": clipped})

    gdf = gpd.GeoDataFrame(rows, crs=gdf.crs)
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()
    diced_vertices = int(gdf.geometry.count_coordinates().sum())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GeoJSON")
    return {
        "source": str(source_path),
        "path": str(output_path),
        "dice_cell_degrees": ISOCHRONE_AGOL_DICE_CELL_DEGREES,
        "feature_count": int(len(gdf)),
        "original_vertices": original_vertices,
        "diced_vertices": diced_vertices,
        "method": "intersect each isochrone band with a 0.5-degree grid; no simplification",
    }


def merged_tags(spec: dict[str, Any]) -> str:
    tags: list[str] = []
    for tag in [*DEFAULT_TAGS, *(spec.get("tags") or [])]:
        tag = str(tag).strip().lower()
        if tag and tag not in tags:
            tags.append(tag)
    return ",".join(tags)


def publish_raster_spec(
    gis: GIS,
    portal: str,
    token: str,
    username: str,
    spec: dict[str, Any],
    group_ids: list[str],
    service_name_suffix: str,
) -> dict[str, Any]:
    path = Path(spec["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    profile = read_raster_profile(path)
    output_name = f"{service_name(spec['title'])}{service_name_suffix}"
    print(f"\n=== {spec['key']} ===", flush=True)
    print(f"  raster: {path}", flush=True)
    print(f"  publish as hosted imagery: {output_name}", flush=True)

    for search_title in (spec["title"], output_name):
        existing = find_item_by_title(portal, token, username, search_title, "Image Service")
        if existing:
            item_id = existing["id"]
            print(f"  Reusing existing imagery item with matching title {search_title!r}: {item_id}", flush=True)
            update_raster_item_metadata(
                portal,
                token,
                username,
                item_id,
                {
                    "title": spec["title"],
                    "snippet": spec["snippet"],
                    "description": spec["description"],
                    "base_tags": DEFAULT_TAGS,
                    "tags": spec.get("tags") or [],
                },
            )
            share_raster_item(portal, token, username, item_id, group_ids)
            summary = item_summary(portal, token, item_id)
            summary["source_profile"] = profile
            return summary

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        item = copy_raster(
            input_raster=str(path.resolve()),
            output_name=output_name,
            raster_type_name="Raster Dataset",
            gis=gis,
            future=False,
            tiles_only=False,
            context={"upload_properties": {"displayProgress": True}},
        )
    item_id = getattr(item, "itemid", None) or getattr(item, "id", None)
    if not item_id:
        raise RuntimeError(f"copy_raster returned no item id for {spec['key']}: {item!r}")

    update_raster_item_metadata(
        portal,
        token,
        username,
        item_id,
        {
            "title": spec["title"],
            "snippet": spec["snippet"],
            "description": spec["description"],
            "base_tags": DEFAULT_TAGS,
            "tags": spec.get("tags") or [],
        },
    )
    share_raster_item(portal, token, username, item_id, group_ids)
    summary = item_summary(portal, token, item_id)
    summary["source_profile"] = profile
    print(f"  published imagery item_id={item_id}", flush=True)
    return summary


def publish_feature_spec(
    portal: str,
    token: str,
    username: str,
    identity: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    path = Path(spec["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    features = load_geojson(path)
    geometry_type, fields, prepared = prepare_geojson_features(features)
    esri_features = prepared["features"]

    print(f"\n=== {spec['key']} ===", flush=True)
    print(f"  geojson: {path}", flush=True)
    print(f"  features: {len(esri_features):,}", flush=True)
    print(f"  geometry_type: {geometry_type}", flush=True)

    if not spec.get("item_id"):
        for search_title in (spec["title"], service_name(spec["title"])):
            existing = find_item_by_title(portal, token, username, search_title, "Feature Service")
            if existing:
                spec = dict(spec)
                spec["item_id"] = existing["id"]
                print(
                    f"  Reusing existing feature item with matching title {search_title!r}: {existing['id']}",
                    flush=True,
                )
                break

    publish_spec = dict(spec)
    publish_spec["share"] = {}
    item_id, added = publish_layer(
        portal=portal,
        username=username,
        token=token,
        identity=identity,
        layer=publish_spec,
        default_tags=DEFAULT_TAGS,
        geometry_type=geometry_type,
        fields=fields,
        esri_features=esri_features,
        batch_size=int(spec.get("batch_size") or 1000),
        max_record_count=10000,
    )
    share = spec.get("share") or {}
    group_ids = [str(value).strip() for value in share.get("group_ids", []) if str(value).strip()]
    share_raster_item(portal, token, username, item_id, group_ids)
    summary = item_summary(portal, token, item_id)
    summary["feature_count"] = added
    print(f"  published feature item_id={item_id}, features={added:,}", flush=True)
    return summary


def existing_item_ids(metadata: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for bucket in ("agol_hosted_imagery_items", "agol_hosted_feature_items"):
        for value in (metadata.get(bucket) or {}).values():
            if isinstance(value, dict) and value.get("item_id"):
                ids.append(value["item_id"])
    return ids


def selected_keys(args: argparse.Namespace, specs: list[dict[str, Any]]) -> set[str]:
    available = {spec["key"] for spec in specs}
    if args.only == "all":
        return available
    keys = {key.strip() for key in args.only.split(",") if key.strip()}
    unknown = sorted(keys - available)
    if unknown:
        raise ValueError(f"Unknown --only key(s): {unknown}. Available keys: {sorted(available)}")
    return keys


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"updated metadata {path}", flush=True)


def main() -> int:
    args = parse_args()
    load_dotenv()
    portal = os.getenv("AGOL_PORTAL_URL", DEFAULT_PORTAL).rstrip("/")
    client_id = os.getenv("AGOL_CLIENT_ID", "")
    redirect_uri = os.getenv("AGOL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    if not client_id:
        raise RuntimeError("Set AGOL_CLIENT_ID in .env or the environment before publishing with browser auth.")

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    iso_agol_copies = {
        "walk": prepare_agol_isochrone_copy(Path(metadata["outputs"]["walk"]["isochrones"])),
        "least_cost": prepare_agol_isochrone_copy(Path(metadata["outputs"]["least_cost"]["isochrones"])),
    }
    if metadata["outputs"]["walk"].get("isochrones_coarse"):
        iso_agol_copies["walk_coarse"] = prepare_agol_isochrone_copy(
            Path(metadata["outputs"]["walk"]["isochrones_coarse"])
        )
    if metadata["outputs"]["least_cost"].get("isochrones_coarse"):
        iso_agol_copies["least_cost_coarse"] = prepare_agol_isochrone_copy(
            Path(metadata["outputs"]["least_cost"]["isochrones_coarse"])
        )
    metadata["agol_publishing_copies"] = {"isochrones": iso_agol_copies}
    write_metadata(args.metadata, metadata)

    token = get_agol_browser_token(portal, client_id, redirect_uri)
    identity = validate_agol_user(portal, token)
    username = identity["username"]
    print(f"AGOL browser user validated: {username} ({identity.get('role')})", flush=True)
    gis = GIS(portal, token=token)

    delete_ids = list(args.delete_item_id)
    if args.delete_existing:
        delete_ids.extend(existing_item_ids(metadata))
    for item_id in dict.fromkeys(delete_ids):
        print(f"delete obsolete item {item_id}", flush=True)
        delete_item(portal, token, username, item_id)

    imagery: dict[str, Any] = dict(metadata.get("agol_hosted_imagery_items") or {})
    features: dict[str, Any] = dict(metadata.get("agol_hosted_feature_items") or {})
    all_specs = [*raster_specs(metadata), *feature_specs(metadata)]
    keys_to_publish = selected_keys(args, all_specs)

    for spec in raster_specs(metadata):
        if spec["key"] not in keys_to_publish:
            continue
        imagery[spec["key"]] = publish_raster_spec(
            gis,
            portal,
            token,
            username,
            spec,
            args.group_id,
            args.service_name_suffix,
        )
        metadata["agol_hosted_imagery_items"] = imagery
        metadata["agol_hosted_feature_items"] = features
        write_metadata(args.metadata, metadata)

    for spec in feature_specs(metadata):
        if spec["key"] not in keys_to_publish:
            continue
        features[spec["key"]] = publish_feature_spec(portal, token, username, identity, spec)
        metadata["agol_hosted_imagery_items"] = imagery
        metadata["agol_hosted_feature_items"] = features
        write_metadata(args.metadata, metadata)

    metadata["agol_hosted_imagery_items"] = imagery
    metadata["agol_hosted_feature_items"] = features
    metadata["agol_publish_method"] = {
        "auth": "browser OAuth",
        "imagery_tool": "arcgis.raster.analytics.copy_raster",
        "feature_tool": "ArcGIS REST createService/addToDefinition/applyEdits",
        "group_ids": args.group_id,
    }
    write_metadata(args.metadata, metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
