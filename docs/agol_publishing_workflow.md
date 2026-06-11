# ArcGIS Online Publishing Workflow

This repo has two publishing-related paths:

1. `ACLED_AGOL_Data_Pipeline.ipynb` shows the original ArcGIS Online REST pattern.
2. `scripts/publish_geojson_to_agol.py` is the reusable publisher for local GeoJSON layers.

## The Existing Notebook Pattern

The useful publishing cells in `ACLED_AGOL_Data_Pipeline.ipynb` are cells 8-10.

### Cell 8: Authenticate

Cell 8 gets an ArcGIS Online token and validates the current user.

Key functions:

- `agol_request(...)`: wrapper around ArcGIS REST `GET` and `POST` calls.
- `get_agol_user_token(...)`: calls `/sharing/rest/generateToken`.
- `validate_agol_user(...)`: confirms the token belongs to a user who can access content.

The PIU publishing script uses the same idea, but prefers OAuth app credentials:

- `AGOL_CLIENT_ID`
- `AGOL_CLIENT_SECRET`
- `AGOL_PORTAL_URL` optional

Username/password credentials are still supported as a fallback:

- `AGOL_USERNAME`
- `AGOL_PASSWORD`

The script also loads a local `.env` file when present. `.env` is gitignored; keep real secrets there or in your shell environment, not in tracked source files.

### Cell 9: Create, Clear, Append, Update

Cell 9 defines the actual hosted feature layer lifecycle.

Key functions:

- `create_feature_service(...)`: creates the hosted feature service container.
- `add_layer_to_service(...)`: adds the feature layer schema.
- `clear_layer(...)`: removes existing features before reloading.
- `append_spark_features(...)`: appends feature records in batches.
- `update_item_metadata(...)`: updates title, description, tags, and license fields.
- `publish_target(...)`: orchestrates the full operation.

The original notebook is ACLED-specific: it expects Spark rows with latitude and longitude and creates a point layer.

The PIU layer is different: it is a local GeoJSON polygon layer. The reusable publisher therefore replaces the Spark-specific pieces with GeoJSON-specific logic.

### Cell 10: Dry Run or Publish

Cell 10 decides whether to write to ArcGIS Online.

The safe pattern is:

1. Dry run first.
2. Confirm the item and feature count.
3. Publish only when the dry run looks right.

The new publisher follows the same pattern: dry run is the default, and `--publish` is required to write to ArcGIS Online.

## Reusable GeoJSON Publisher

Layer uploads are registered in:

```text
data/layers/agol_layers.json
```

The first registered layer is:

```text
piu_project_locations_adm3
```

It points to:

```text
data/processed/piu_project_locations_adm3.geojson
```

That GeoJSON is produced by:

```bash
python3 scripts/build_piu_adm3_locations.py
```

## Local Dry Run

Validate the registered PIU layer without ArcGIS credentials:

```bash
python3 scripts/publish_geojson_to_agol.py
```

Expected result:

```text
features:      361
geometry_type: esriGeometryPolygon
fields:        19
```

## ArcGIS Dry Run

Validate credentials and check whether the hosted feature service already exists:

```bash
export AGOL_PORTAL_URL="https://geowb.maps.arcgis.com"
export AGOL_CLIENT_ID="your_client_id"
export AGOL_CLIENT_SECRET="your_client_secret"

python3 scripts/publish_geojson_to_agol.py
```

You can also put those same values in a local `.env` file. Use `.env.example` as the template.

If `item_id` is blank in the registry, the dry run searches for an existing feature service with the same title.

## Publish

Publish the PIU layer:

```bash
python3 scripts/publish_geojson_to_agol.py --publish
```

On first publish, the script creates a hosted feature service. It prints the new `item_id`.

Add that `item_id` back to `data/layers/agol_layers.json` so future runs update the same hosted layer.

## Adding Future Layers

For each new layer:

1. Produce a GeoJSON file.
2. Add a new entry under `layers` in `data/layers/agol_layers.json`.
3. Run a dry run:

```bash
python3 scripts/publish_geojson_to_agol.py --layer your_layer_key
```

4. Publish:

```bash
python3 scripts/publish_geojson_to_agol.py --layer your_layer_key --publish
```

To publish every registered layer:

```bash
python3 scripts/publish_geojson_to_agol.py --layer all --publish
```
