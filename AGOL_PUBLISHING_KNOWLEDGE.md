# AGOL Publishing Knowledge

This note captures practical lessons learned while publishing Ethiopia Geo Hub
layers to ArcGIS Online. It is intentionally focused on AGOL publishing behavior,
not on source-data preparation.

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
