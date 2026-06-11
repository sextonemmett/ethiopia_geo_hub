# ArcGIS Online Publishing Error

We are attempting to publish a hosted feature layer to ArcGIS Online using OAuth 2.0 app credentials.

## Portal

```text
https://geowb.maps.arcgis.com
```

## Target Operation

Create a hosted feature service.

## Failing Endpoint

```text
POST https://geowb.maps.arcgis.com/sharing/rest/content/users/esexton@worldbank.org_geowb/createService
```

## Request Context

The script successfully obtains an OAuth access token using `client_id` and `client_secret`.

Requests include the required referrer header:

```text
Referer: https://geowb.maps.arcgis.com
```

## Failing Code

File:

```text
scripts/publish_geojson_to_agol.py
```

Function:

```text
create_feature_service(...)
```

Code:

```python
result = agol_request(
    "POST",
    f"{portal}/sharing/rest/content/users/{username}/createService",
    context=f"createService failed for {title}",
    token=token,
    data={
        "f": "json",
        "createParameters": json.dumps(create_params),
        "outputType": "featureService",
        **target_metadata(layer, default_tags),
    },
    timeout=60,
)
```

## Create Parameters

```python
{
    "name": "Ethiopia_PIU_Project_Locations_ADM3",
    "serviceDescription": "...",
    "hasStaticData": False,
    "maxRecordCount": 10000,
    "supportedQueryFormats": "JSON",
    "capabilities": "Query,Editing,Create,Update,Delete,Extract",
    "spatialReference": {"wkid": 4326},
    "allowGeometryUpdates": True,
    "units": "esriDecimalDegrees",
}
```

## Item Metadata Payload

```python
{
    "title": "Ethiopia PIU Project Locations - ADM3",
    "snippet": "...",
    "description": "...",
    "tags": "ethiopia,geo hub,piu,portfolio,projects,adm3,woreda",
}
```

## ArcGIS Online Response

```json
{
  "error": {
    "code": 403,
    "messageCode": "GWM_0003",
    "message": "You do not have permissions to access this resource or perform this operation.",
    "details": []
  }
}
```

## OAuth App Privilege Metadata Observed

The OAuth token metadata shows location-service privileges, including basemaps, geocoding, routing, and geoenrichment.

The observed token metadata does not show these content publishing privileges:

```text
portal:user:createItem
portal:publisher:publishFeatures
```
