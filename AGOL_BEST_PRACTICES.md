# AGOL Best Practices

This note captures practical lessons learned while publishing Ethiopia Geo Hub
layers to ArcGIS Online. It is intentionally focused on AGOL publishing behavior,
not on source-data preparation.

## Ethiopia Geo Hub Defaults

All Ethiopia Geo Hub uploads should include these tags:

```text
eth
foundational_data
```

They should also be shared with:

```text
Ethiopia Geospatial Data Hub Core Team
31731315dc3a4f92adebf919c4651345
```

For reusable scripts, make these defaults explicit rather than relying on a
manual AGOL follow-up.

## Source Metadata

Every published hazard or raster layer should have a companion local metadata
JSON near the source data. Record at minimum:

- source dataset title and provider;
- source file path(s);
- date, vintage, scenario, return period, or other product identifiers visible
  in filenames or source documentation;
- units and value semantics;
- license and attribution text from the source package;
- local processing steps, including boundary clip, thresholding, nodata changes,
  dtype conversion, mosaicking, and any deleted/replaced AGOL item IDs;
- final AGOL item ID, URL, item type, pixel type, pixel size, and validation
  notes.

Do not rely on AGOL item descriptions alone. Keep the local metadata JSON as the
rebuild/audit record, then mirror the important source title, provider, units,
license, and processing summary into the AGOL item metadata.

## Authentication

- Use browser authentication when creating new hosted content.
  - `--auth browser` produces a real user token with the user's AGOL privileges.
  - App/client-credentials tokens can read or update some owned resources, but
    may fail on content creation with `You do not have permissions to access
    this resource or perform this operation`.
- The browser auth flow used in this repo opens an ArcGIS OAuth URL and waits
  for the localhost redirect at `http://127.0.0.1:8765/callback`.
- If the browser does not open automatically, copy the printed URL into a
  browser and complete the sign-in.

## Hosted Feature Services

The GeoJSON feature-layer workflow works well for points and polygons.

Working path:

```bash
uv run python scripts/publish_geojson_to_agol.py --layer kobo_project_woreda_points --auth browser --publish
```

The publisher:

- reads a local GeoJSON;
- converts features and fields to Esri JSON;
- creates or reuses a hosted Feature Service;
- adds a layer definition when needed;
- clears existing features;
- appends replacement features in batches;
- updates item metadata;
- shares the item if `group_ids` are configured.

Registry entries live in:

```text
data/layers/agol_layers.json
```

For group sharing, use:

```json
"share": {
  "org": false,
  "everyone": false,
  "group_ids": ["GROUP_ID"]
}
```

Useful dry run:

```bash
uv run python scripts/publish_geojson_to_agol.py --layer kobo_project_woreda_points
```

This validates local geometry, field conversion, and AGOL item access without
writing features.

Dense polygon layers can exceed AGOL's `applyEdits` request-size limit even
when the feature count is small. If a publish fails with HTTP 413, keep the
original source file intact, create a clearly named AGOL publishing copy, repair
geometry validity after simplification, and set a per-layer `batch_size` in
`data/layers/agol_layers.json` so large features are uploaded in smaller
requests:

```json
"path": "data/basins/agro_climate_regions_agol.geojson",
"batch_size": 1
```

For dissolved national isochrone layers, feature count can be misleading. The
market accessibility isochrones had only 9 features per mode, but the original
GeoJSONs contained hundreds of thousands to more than 1 million coordinates and
failed with:

```text
413 Client Error: Request Entity Too Large ... /applyEdits
```

Aggressive simplification can make national isochrones look visibly poor on a
map. A better working pattern was to preserve the original analysis GeoJSON,
create an `*_agol.geojson` publishing copy, split each band by a regular grid,
repair validity, keep only polygonal geometries, and append with a small batch
size. For the 500 m market accessibility isochrones, splitting by 0.5-degree
grid cells avoided request-size limits without simplifying away the 500 m
detail: the walking layer published as 2,086 features with 691,905 coordinates,
and the least-cost layer published as 2,428 features with 1,570,900 coordinates.
Record the dicing method in local metadata because the hosted Feature Service is
an AGOL upload-friendly representation of the full-resolution analysis artifact.

## Hosted Imagery Layers

GeoTIFF publishing is not the same as GeoJSON publishing.

Working path:

```bash
uv run python scripts/publish_worldpop_rasters_to_agol.py
```

The working imagery workflow uses the ArcGIS Python API:

```python
arcgis.raster.analytics.copy_raster(
    input_raster="/path/to/local.tif",
    output_name="Image_Service_Name",
    raster_type_name="Raster Dataset",
    tiles_only=False,
)
```

Important details:

- Pass the local GeoTIFF path directly to `copy_raster`.
- Let the ArcGIS Python API upload the file to the AGOL raster user store.
- Use `raster_type_name="Raster Dataset"`.
- Use `tiles_only=False` when a dynamic Imagery Layer / Image Service is wanted.
- Validate the output ImageServer metadata after publishing.
- For tiled raster products, prefer building one local mosaic GeoTIFF and
  publishing that single file.

For the WorldPop 1km rasters, valid output metadata looked like:

```text
pixelSizeX: 0.0083333333
pixelSizeY: 0.0083333333
pixelType: F32
bandCount: 1
extent: Ethiopia bounds, roughly 33E-48E and 3.4N-14.9N
```

The publisher rejects outputs with invalid extent, invalid cell size, or
non-float pixel type.

For the Fathom fluvial undefended 1-in-100-year flood raster, valid output
metadata looked like:

```text
pixelSizeX: 0.000277777777777778
pixelSizeY: 0.000277777777777778
pixelType: U8
extent: clipped Ethiopia bounds, roughly 33E-48E and 3.4N-14.9N
```

That source arrived as 131 one-degree COG tiles with signed 16-bit values and
`-32767` no-data pixels but no TIFF no-data tag. The working publisher builds a
single DEFLATE-compressed BigTIFF mosaic with `nodata=-32767`, derives a clipped
binary mask, then passes that single local mask file to `copy_raster`.

For the published binary flood mask, use `uint8` values:

```text
1: source depth is greater than or equal to the chosen threshold, for example 10 cm
0: source depth is less than the chosen threshold inside Ethiopia
255: no-data outside the dissolved Ethiopia boundary
```

Using `255` as no-data keeps valid binary values limited to `0` and `1`, and
AGOL preserves the output as `pixelType: U8`.

For the GEM GSHM PGA 475-year rock earthquake raster, the source grid had
slightly non-square geographic cells:

```text
pixelSizeX: 0.05
pixelSizeY: 0.049996666666667
pixelType: float32 after local clipping
```

AGOL Copy Raster normalized the hosted Image Service to square cells of about
`0.049996666666667` degrees in both directions. The service extent stayed within
roughly half a source pixel of the local clipped raster extent, and the output
validated as `pixelType: F32`.

## Imagery Pitfalls

Do not publish GeoTIFFs as generic AGOL `Image` items and then feed those item
IDs into raster analysis.

This failed path looked like:

1. Upload GeoTIFF with generic REST `addItem(type="Image")`.
2. Pass the resulting item ID into `CreateImageCollection` or raw `CopyRaster`.

The uploaded source items looked harmless but were incomplete as geospatial
inputs:

```text
type: Image
extent: []
spatialReference: null
url: null
```

The resulting Image Services were corrupt:

```text
pixelSizeX/Y: 10600614 degrees
pixelType: U8
extent: invalid mixed degree/meter-looking values
```

Symptoms in AGOL:

- layer renders blank;
- "Zoom to layer" jumps to an absurd/global extent;
- item overview reports impossible cell size;
- source type may appear as generic image;
- pixel type is downgraded from `float32` to unsigned char.

Root cause:

AGOL interpreted the uploaded GeoTIFF as a generic image file instead of a
georeferenced raster dataset. The raster-analysis service then built an image
collection from bad source metadata.

Do not assume a list of local GeoTIFF paths will publish cleanly as a single
AGOL image layer, even though `copy_raster` accepts a list. For the 131 Fathom
flood tiles, the upload completed, but AGOL failed while building mosaic dataset
items with:

```text
Failed to open raster dataset: CURL error: Could not resolve host: stg-arcgisazureimagery.blob.https
```

Creating one local mosaic first avoided that AGOL raster-store failure and
produced a validated Image Service.

Small tile lists can still be useful when a single GeoTIFF repeatedly stalls
inside the ArcGIS Python API upload thread. For the Ethiopia market
least-cost accessibility raster, the single 12 MB GeoTIFF and a clean 16 MB
staging copy both stalled at `0/1 files` during `_upload_imagery_agol`, while a
512x512 diagnostic tile uploaded successfully. Splitting the full raster into
four adjacent GeoTIFF tiles of roughly 1-4 MB each and passing that four-path
list to `copy_raster` uploaded all files quickly and produced a valid `F32`
Image Service with 500 m pixels. Use this as a targeted workaround for small
national rasters, not as a default for large many-tile collections.

After publishing, validate hosted raster values in the Image Service's reported
spatial reference, not necessarily the source raster's spatial reference. AGOL
may publish one hosted imagery item in the source projection and another in Web
Mercator even when both came from compatible local rasters and display correctly
in Map Viewer. In the Ethiopia market accessibility case, the walking Image
Service reported the source Ethiopia Albers grid, while the tiled least-cost
Image Service reported Web Mercator. Querying the least-cost service with source
Albers coordinates returned `NoData`; querying the same locations transformed to
Web Mercator returned values matching the local GeoTIFF. For side-by-side map
review, use shared numeric class breaks or a shared stretch because AGOL's
default independent stretches can make a lower-valued raster look visually
"slower" than a higher-valued raster.

`copy_raster` can also be interrupted after AGOL has created or reserved an
Image Service. A rerun may fail with "An Image Service by this name already
exists" even if the local metadata was not updated before interruption. For
resumable raster publishers, search for both the clean item title and the
service-name form with underscores, then reuse the existing Image Service when
it is valid instead of trying to create a duplicate.

When adding a missing no-data tag to temporary COG copies, GDAL/rasterio may
refuse update mode unless `IGNORE_COG_LAYOUT_BREAK=YES` is used. Prefer building
a new derived mosaic instead of editing source COGs in place.

## Image Service Names

After deleting a hosted Image Service, AGOL may continue to reserve the service
name for a while.

If a republish fails with:

```text
An Image Service by this name already exists
```

use a service-name suffix such as:

```bash
uv run python scripts/publish_worldpop_rasters_to_agol.py --service-name-suffix _Corrected
```

The service URL will include the suffix, but item metadata can still use a clean
human-readable title.

## Sharing

To list available groups:

```bash
uv run python scripts/publish_geojson_to_agol.py --auth browser --list-groups
```

For one-off sharing, use the AGOL item `share` endpoint with a browser user
token. Share the final hosted service items, not intermediate source upload
items, unless users specifically need the raw files.

For WorldPop, the final items to share are Image Service items. The intermediate
uploaded GeoTIFF source items are not the map-ready layer outputs.

## Validation Checklist

For Feature Services:

- item type is `Feature Service`;
- service URL ends in `/FeatureServer`;
- geometry type is expected;
- feature count matches local output;
- fields are expected;
- sharing is correct.

For Image Services:

- item type is `Image Service`;
- service URL ends in `/ImageServer`;
- extent matches source raster bounds;
- cell size matches source raster resolution;
- pixel type matches source raster intent, for example `F32` for WorldPop;
- layer renders at normal map scales;
- sharing is correct.

## Current Recommended Pattern

- GeoJSON points/polygons: use `scripts/publish_geojson_to_agol.py`.
- GeoTIFF rasters: use `scripts/publish_worldpop_rasters_to_agol.py`.
- New content creation: prefer `--auth browser` / browser user token.
- Do not use generic REST `addItem(type="Image")` as the source for hosted
  imagery publishing.
