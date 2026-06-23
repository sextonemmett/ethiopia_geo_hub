#!/usr/bin/env python3
"""Build a flood-and-topography background visual for the Ethiopia Geo Hub."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
import numpy as np
from PIL import Image, ImageFilter
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEM = ROOT / "data/elevation/Elevation/DEM_definedcoordvrt1.tif"
DEFAULT_FLOOD = (
    ROOT
    / "data/hazards/floods/products/"
    "fathom_fluvial_undefended_1in100_2030_ssp2_45_ethiopia_binary_10cm.tif"
)
DEFAULT_BOUNDARY = ROOT / "data/admin_boundaries/boundaries_1.geojson"
DEFAULT_OUTPUT_DIR = ROOT / "outputs/geo_hub_background"
DEFAULT_PRESENTATION = ROOT / "presentations/geo_hub_background_preview.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    parser.add_argument("--flood-mask", type=Path, default=DEFAULT_FLOOD)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--preview-html", type=Path, default=DEFAULT_PRESENTATION)
    parser.add_argument("--width", type=int, default=3200)
    parser.add_argument("--height", type=int, default=1800)
    return parser.parse_args()


def normalized(values: np.ndarray, valid: np.ndarray, lower: float = 2, upper: float = 98) -> np.ndarray:
    low, high = np.nanpercentile(values[valid], [lower, upper])
    scaled = (values - low) / (high - low)
    return np.clip(scaled, 0, 1)


def hillshade(elevation: np.ndarray, valid: np.ndarray, azimuth: float = 315, altitude: float = 42) -> np.ndarray:
    filled = elevation.copy()
    filled[~valid] = np.nanmedian(elevation[valid])
    dy, dx = np.gradient(filled)
    slope = np.pi / 2 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    azimuth_rad = np.deg2rad(360 - azimuth + 90)
    altitude_rad = np.deg2rad(altitude)
    shaded = (
        np.sin(altitude_rad) * np.sin(slope)
        + np.cos(altitude_rad) * np.cos(slope) * np.cos(azimuth_rad - aspect)
    )
    shaded = np.clip((shaded + 1) / 2, 0, 1)
    shaded[~valid] = 0
    return shaded


def read_dem(path: Path) -> tuple[np.ndarray, np.ndarray, rasterio.Affine, rasterio.crs.CRS, tuple[float, float, float, float]]:
    with rasterio.open(path) as dataset:
        elevation = dataset.read(1).astype("float32")
        valid = elevation != dataset.nodata if dataset.nodata is not None else np.isfinite(elevation)
        bounds = (dataset.bounds.left, dataset.bounds.bottom, dataset.bounds.right, dataset.bounds.top)
        return elevation, valid, dataset.transform, dataset.crs, bounds


def reproject_flood_to_dem(
    flood_path: Path,
    shape: tuple[int, int],
    transform: rasterio.Affine,
    crs: rasterio.crs.CRS,
) -> np.ndarray:
    flood = np.zeros(shape, dtype="uint8")
    with rasterio.open(flood_path) as source:
        reproject(
            source=rasterio.band(source, 1),
            destination=flood,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=transform,
            dst_crs=crs,
            dst_nodata=0,
            resampling=Resampling.max,
        )
    return flood == 1


def boundary_mask_and_path(
    boundary_path: Path,
    shape: tuple[int, int],
    transform: rasterio.Affine,
    crs: rasterio.crs.CRS,
) -> tuple[np.ndarray, list[MplPath]]:
    boundary = gpd.read_file(boundary_path).to_crs(crs)
    geometry = boundary.union_all()
    mask = geometry_mask([geometry], out_shape=shape, transform=transform, invert=True)
    paths: list[MplPath] = []

    for polygon in getattr(geometry, "geoms", [geometry]):
        exterior = np.asarray(polygon.exterior.coords)
        codes = np.full(len(exterior), MplPath.LINETO)
        codes[0] = MplPath.MOVETO
        paths.append(MplPath(exterior, codes))
    return mask, paths


def compose_visual(
    elevation: np.ndarray,
    valid: np.ndarray,
    flood: np.ndarray,
    country_mask: np.ndarray,
    width: int,
    height: int,
) -> Image.Image:
    data_mask = valid & country_mask
    relief = hillshade(elevation, data_mask)
    elev_norm = normalized(elevation, data_mask)
    flood_distance = Image.fromarray((flood & data_mask).astype("uint8") * 255).filter(
        ImageFilter.GaussianBlur(radius=2.2)
    )
    flood_glow = np.asarray(flood_distance).astype("float32") / 255

    terrain = LinearSegmentedColormap.from_list(
        "ethiopia_hub_terrain",
        ["#071820", "#12322f", "#31543a", "#7c7a4b", "#c79f60", "#ebe2c1"],
    )(elev_norm)[..., :3]
    shade = 0.48 + 0.78 * relief[..., None]
    rgb = np.clip(terrain * shade, 0, 1)

    deep_water = np.array([0.04, 0.48, 0.66])
    hot_water = np.array([0.26, 0.91, 0.96])
    flood_strength = np.clip((flood.astype("float32") * 0.72 + flood_glow * 0.52) * data_mask, 0, 0.9)
    rgb = rgb * (1 - flood_strength[..., None]) + hot_water * flood_strength[..., None]
    rgb = rgb * (1 - (flood_glow * 0.24)[..., None]) + deep_water * (flood_glow * 0.24)[..., None]

    alpha = np.where(data_mask, 1.0, 0.0)
    rgba = np.dstack([rgb, alpha])
    image = Image.fromarray((rgba * 255).astype("uint8"), mode="RGBA")

    canvas = Image.new("RGBA", image.size, "#061014")
    glow = image.filter(ImageFilter.GaussianBlur(radius=26))
    canvas.alpha_composite(glow)
    canvas.alpha_composite(image)
    return canvas.resize((width, height), Image.Resampling.LANCZOS)


def draw_finishing_layers(
    image: Image.Image,
    output_path: Path,
    bounds: tuple[float, float, float, float],
    boundary_paths: list[MplPath],
) -> None:
    xmin, ymin, xmax, ymax = bounds
    fig = plt.figure(figsize=(image.width / 200, image.height / 200), dpi=200)
    fig.patch.set_facecolor("#061014")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(image, extent=(xmin, xmax, ymin, ymax), origin="upper")
    ax.set_aspect("auto")

    for path in boundary_paths:
        ax.add_patch(
            PathPatch(
                path,
                facecolor="none",
                edgecolor=(0.9, 0.98, 0.95, 0.58),
                linewidth=1.1,
                joinstyle="round",
                capstyle="round",
            )
        )

    ax.scatter(
        [38.7578],
        [8.9806],
        s=18,
        c="#f4d35e",
        edgecolors=(1, 1, 1, 0.5),
        linewidths=0.7,
        alpha=0.88,
        zorder=5,
    )

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.axis("off")
    fig.savefig(output_path, dpi=200, transparent=False, facecolor=fig.get_facecolor())
    plt.close(fig)


def write_preview(path: Path, background_path: Path) -> None:
    relative_background = Path(os.path.relpath(background_path, path.parent))
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ethiopia Geo Hub Background Preview</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f6f0df;
      --muted: rgba(246, 240, 223, 0.72);
      --accent: #45d2d8;
    }}

    * {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: Aptos, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
      background: #061014;
    }}

    .hero {{
      min-height: 100vh;
      display: grid;
      align-items: center;
      padding: clamp(28px, 7vw, 108px);
      position: relative;
      overflow: hidden;
      isolation: isolate;
    }}

    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(6, 16, 20, 0.92) 0%, rgba(6, 16, 20, 0.58) 44%, rgba(6, 16, 20, 0.18) 100%),
        linear-gradient(0deg, rgba(6, 16, 20, 0.96) 0%, rgba(6, 16, 20, 0.1) 42%, rgba(6, 16, 20, 0.54) 100%),
        url("{relative_background.as_posix()}") center / cover no-repeat;
      z-index: -2;
    }}

    .hero::after {{
      content: "";
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.045) 1px, transparent 1px);
      background-size: 96px 96px;
      mask-image: linear-gradient(90deg, black, transparent 72%);
      z-index: -1;
    }}

    .content {{
      max-width: 820px;
    }}

    .eyebrow {{
      margin-bottom: 18px;
      color: var(--accent);
      font-size: 15px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    h1 {{
      margin: 0;
      max-width: 760px;
      font-size: clamp(52px, 8vw, 112px);
      line-height: 0.94;
      letter-spacing: 0;
      font-weight: 850;
    }}

    p {{
      max-width: 620px;
      margin: 24px 0 0;
      color: var(--muted);
      font-size: clamp(18px, 2vw, 27px);
      line-height: 1.24;
      font-weight: 520;
    }}
  </style>
</head>
<body>
  <main class="hero">
    <section class="content" aria-label="Ethiopia Geospatial Data Hub">
      <div class="eyebrow">Ethiopia Geospatial Data Hub</div>
      <h1>Spatial data for decisions at terrain scale.</h1>
      <p>Flood exposure and highland relief rendered from the hub's own source layers.</p>
    </section>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.preview_html.parent.mkdir(parents=True, exist_ok=True)

    elevation, dem_valid, transform, crs, bounds = read_dem(args.dem)
    country_mask, boundary_paths = boundary_mask_and_path(args.boundary, elevation.shape, transform, crs)
    flood = reproject_flood_to_dem(args.flood_mask, elevation.shape, transform, crs)
    image = compose_visual(elevation, dem_valid, flood, country_mask, args.width, args.height)

    output_path = args.output_dir / "ethiopia_flood_topography_background.png"
    draw_finishing_layers(image, output_path, bounds, boundary_paths)
    write_preview(args.preview_html, output_path)
    print(output_path.relative_to(ROOT))
    print(args.preview_html.relative_to(ROOT))


if __name__ == "__main__":
    main()
