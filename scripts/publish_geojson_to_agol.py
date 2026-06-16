#!/usr/bin/env python3
"""Publish registered GeoJSON layers to ArcGIS Online hosted feature services.

By default this script performs a dry run. Use --publish to create/reuse a
hosted feature service, clear the first layer, and append the GeoJSON features.

Credentials are read from environment variables:
  AGOL_CLIENT_ID
  AGOL_CLIENT_SECRET
  AGOL_USERNAME
  AGOL_PASSWORD
  AGOL_REDIRECT_URI  optional, used by --auth browser
  AGOL_PORTAL_URL  optional, defaults to the registry portal value

A local .env file is also loaded if present. Keep .env untracked.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests


DEFAULT_REGISTRY = Path("data/layers/agol_layers.json")
PLACEHOLDER_VALUES = {"", "your_username", "your_password", "PASTE_HERE"}
RESERVED_FIELD_NAMES = {
    "fid",
    "oid",
    "objectid",
    "shape",
    "shape_area",
    "shape_length",
    "globalid",
}


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


def configured_secret(value: str) -> bool:
    return bool(value) and value not in PLACEHOLDER_VALUES and not value.startswith("PASTE_")


def raise_agol_error(context: str, payload: dict[str, Any]) -> None:
    if "error" not in payload:
        return
    error = payload["error"]
    if isinstance(error, dict):
        message = error.get("message") or error.get("error_description") or error
        details = error.get("details") or []
        raise RuntimeError(f"{context}: {message}; details={details}; raw={error}")
    raise RuntimeError(f"{context}: {error}")


def agol_request(
    method: str,
    url: str,
    *,
    context: str,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    params = dict(params or {})
    data = dict(data or {})
    headers = {}
    referer = os.getenv("AGOL_REFERER", "")
    if referer:
        headers["Referer"] = referer
    if token:
        if method.upper() == "GET":
            params["token"] = token
        else:
            data["token"] = token
    if method.upper() == "GET":
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
    else:
        response = requests.post(url, data=data, headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "")
        prefix = response.text[:200].replace("\n", " ")
        raise RuntimeError(
            f"{context}: ArcGIS returned a non-JSON response from {url}. "
            f"content-type={content_type!r}; prefix={prefix!r}"
        ) from exc
    raise_agol_error(context, payload)
    return payload


def get_agol_user_token(portal: str, username: str, password: str, expiration: int = 120) -> str:
    payload = agol_request(
        "POST",
        f"{portal}/sharing/rest/generateToken",
        context="AGOL username/password login failed",
        data={
            "username": username,
            "password": password,
            "client": "referer",
            "referer": portal,
            "expiration": expiration,
            "f": "json",
        },
    )
    token = payload.get("token")
    if not token:
        raise RuntimeError(f"AGOL login returned no token: {payload}")
    return token


def get_agol_client_token(client_id: str, client_secret: str) -> str:
    payload = agol_request(
        "POST",
        "https://www.arcgis.com/sharing/rest/oauth2/token",
        context="AGOL client credentials login failed",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "f": "json",
        },
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"AGOL client credentials login returned no access token: {payload}")
    return token


def _oauth_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _extract_oauth_code(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        return (query.get("code") or [""])[0]
    return value


def _localhost_redirect_parts(redirect_uri: str) -> tuple[str, int] | None:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        return None
    hostname = parsed.hostname or ""
    if hostname not in {"127.0.0.1", "localhost"}:
        return None
    return hostname, parsed.port or 80


def _wait_for_local_oauth_redirect(redirect_uri: str, state: str, timeout_seconds: int = 300) -> str:
    local_parts = _localhost_redirect_parts(redirect_uri)
    if not local_parts:
        return ""
    host, port = local_parts

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            self.server.oauth_query = query  # type: ignore[attr-defined]
            if query.get("error"):
                status = 400
                body = "ArcGIS sign-in failed. You can close this tab and return to the terminal."
            elif query.get("state") != state:
                status = 400
                body = "ArcGIS sign-in returned an invalid state. You can close this tab and return to the terminal."
            elif query.get("code"):
                status = 200
                body = "ArcGIS sign-in complete. You can close this tab and return to the terminal."
            else:
                status = 400
                body = "ArcGIS sign-in returned no authorization code. You can close this tab and return to the terminal."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer((host, port), OAuthCallbackHandler)
    server.timeout = timeout_seconds
    try:
        server.handle_request()
        query = getattr(server, "oauth_query", {})
    finally:
        server.server_close()

    if not query:
        raise RuntimeError("Timed out waiting for the AGOL browser sign-in redirect.")
    if query.get("error"):
        raise RuntimeError(f"AGOL browser sign-in failed: {query.get('error_description') or query.get('error')}")
    if query.get("state") != state:
        raise RuntimeError("AGOL browser sign-in returned an invalid state.")
    return query.get("code", "")


def get_agol_browser_token(portal: str, client_id: str, redirect_uri: str) -> str:
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    authorize_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge": _oauth_code_challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
        "expiration": "20160",
    }
    authorize_url = f"{portal}/sharing/rest/oauth2/authorize?{urlencode(authorize_params)}"
    print("Opening ArcGIS sign-in page in your browser.")
    print(f"If the browser does not open, visit this URL:\n{authorize_url}")

    local_redirect = _localhost_redirect_parts(redirect_uri)
    if local_redirect:
        webbrowser.open(authorize_url)
        code = _wait_for_local_oauth_redirect(redirect_uri, state)
    else:
        webbrowser.open(authorize_url)
        print("After sign-in, paste the full redirected URL or just the authorization code.")
        code = _extract_oauth_code(input("Authorization code: "))

    if not code:
        raise RuntimeError("AGOL browser sign-in did not return an authorization code.")

    payload = agol_request(
        "POST",
        f"{portal}/sharing/rest/oauth2/token",
        context="AGOL browser sign-in token exchange failed",
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "f": "json",
        },
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"AGOL browser sign-in returned no access token: {payload}")
    return token


def validate_agol_user(portal: str, token: str) -> dict[str, Any]:
    self_info = agol_request(
        "GET",
        f"{portal}/sharing/rest/community/self",
        context="AGOL /community/self failed",
        token=token,
        params={"f": "json"},
    )
    username = self_info.get("username")
    if not username:
        raise RuntimeError(f"AGOL did not return an authenticated username: {self_info}")
    content = agol_request(
        "GET",
        f"{portal}/sharing/rest/content/users/{username}",
        context=f"Cannot access AGOL content for {username}",
        token=token,
        params={"f": "json", "num": 1},
    )
    return {
        "username": username,
        "full_name": self_info.get("fullName"),
        "role": self_info.get("role"),
        "folders": len(content.get("folders", []) or []),
    }


def validate_agol_token_owner(portal: str, token: str) -> dict[str, Any]:
    self_info = agol_request(
        "GET",
        f"{portal}/sharing/rest/community/self",
        context="AGOL /community/self failed",
        token=token,
        params={"f": "json"},
    )
    username = self_info.get("username")
    if username:
        return validate_agol_user(portal, token)

    app_info = self_info.get("appInfo") or {}
    app_id = app_info.get("appId") or self_info.get("client_id") or self_info.get("id") or app_info.get("itemId")
    if not app_id:
        raise RuntimeError(f"AGOL token did not identify a user or OAuth app: {self_info}")
    app_item = {}
    if app_info.get("itemId"):
        app_item = get_item(portal, token, app_info["itemId"])
    owner = app_item.get("owner") or app_id
    return {
        "username": owner,
        "full_name": self_info.get("fullName") or "OAuth application",
        "role": self_info.get("role") or "oauth_app",
        "folders": 0,
        "is_app_token": True,
        "app_id": app_id,
        "app_item_id": app_info.get("itemId", ""),
        "app_privileges": app_info.get("privileges") or [],
    }


def list_user_groups(portal: str, token: str, username: str) -> list[dict[str, Any]]:
    payload = agol_request(
        "GET",
        f"{portal}/sharing/rest/community/users/{username}",
        context=f"Could not list AGOL groups for {username}",
        token=token,
        params={"f": "json"},
    )
    return payload.get("groups") or []


def get_item(portal: str, token: str, item_id: str) -> dict[str, Any]:
    return agol_request(
        "GET",
        f"{portal}/sharing/rest/content/items/{item_id}",
        context=f"Item lookup failed for {item_id}",
        token=token,
        params={"f": "json"},
    )


def find_feature_service_by_title(portal: str, token: str, username: str, title: str) -> dict[str, Any] | None:
    payload = agol_request(
        "GET",
        f"{portal}/sharing/rest/search",
        context=f"AGOL search failed for {title}",
        token=token,
        params={
            "f": "json",
            "q": f'title:"{title}" AND owner:{username} AND type:"Feature Service"',
            "num": 10,
        },
    )
    matches = [item for item in payload.get("results", []) if (item.get("title") or "").strip() == title]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.get("modified") or 0, reverse=True)[0]


def validate_item_permission(portal: str, token: str, username: str, identity: dict[str, Any], item_id: str) -> dict[str, Any]:
    item = get_item(portal, token, item_id)
    owner = (item.get("owner") or "").lower()
    is_owner = owner == username.lower()
    is_admin = identity.get("role") == "org_admin"
    if not is_owner and not is_admin:
        raise RuntimeError(
            f"AGOL item {item_id} is owned by '{item.get('owner')}', "
            f"but authenticated user is '{username}' and is not org_admin."
        )
    if item.get("type") != "Feature Service":
        raise RuntimeError(f"AGOL item {item_id} is type '{item.get('type')}', expected Feature Service.")
    return item


def service_name(title: str) -> str:
    value = title.replace("&", "and")
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return value[:90] or "geojson_layer"


def target_metadata(layer: dict[str, Any], default_tags: list[str]) -> dict[str, str]:
    tags = []
    for tag in [*default_tags, *(layer.get("tags") or [])]:
        tag = str(tag).strip().lower()
        if tag and tag not in tags:
            tags.append(tag)
    return {
        "title": layer["title"],
        "snippet": layer.get("snippet", ""),
        "description": layer.get("description", ""),
        "tags": ",".join(tags),
        "licenseInfo": layer.get("license_info", ""),
    }


def create_feature_service(
    portal: str,
    username: str,
    token: str,
    layer: dict[str, Any],
    default_tags: list[str],
    max_record_count: int,
) -> tuple[str, str]:
    title = layer["title"]
    create_params = {
        "name": service_name(title),
        "serviceDescription": layer.get("snippet", ""),
        "hasStaticData": False,
        "maxRecordCount": max_record_count,
        "supportedQueryFormats": "JSON",
        "capabilities": "Query,Editing,Create,Update,Delete,Extract",
        "spatialReference": {"wkid": 4326},
        "allowGeometryUpdates": True,
        "units": "esriDecimalDegrees",
        "xssPreventionInfo": {
            "xssPreventionEnabled": True,
            "xssPreventionRule": "InputOnly",
            "xssInputRule": "rejectInvalid",
        },
    }
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
    if not result.get("success"):
        raise RuntimeError(f"createService did not succeed for {title}: {result}")
    return result["itemId"], result["serviceurl"]


def service_info(service_url: str, token: str) -> dict[str, Any]:
    return agol_request(
        "GET",
        service_url,
        context=f"Could not read service info: {service_url}",
        token=token,
        params={"f": "json"},
    )


def admin_service_url(service_url: str) -> str:
    if "/rest/services/" not in service_url:
        raise RuntimeError(f"Cannot derive admin URL from service URL: {service_url}")
    return service_url.replace("/rest/services/", "/rest/admin/services/")


def capability_set(value: str) -> set[str]:
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def capability_string(caps: set[str]) -> str:
    preferred = ["Query", "Create", "Update", "Delete", "Editing", "Extract"]
    ordered = [cap for cap in preferred if cap in caps]
    ordered.extend(sorted(cap for cap in caps if cap not in preferred))
    return ",".join(ordered)


def ensure_service_capabilities(service_url: str, token: str, layer_index: int | None = None) -> None:
    needed = {"Query", "Create", "Update", "Delete", "Editing", "Extract"}
    admin_url = admin_service_url(service_url)
    info = service_info(service_url, token)
    current = capability_set(info.get("capabilities", ""))
    if not needed.issubset(current):
        result = agol_request(
            "POST",
            f"{admin_url}/updateDefinition",
            context=f"Could not update service capabilities for {service_url}",
            token=token,
            data={"f": "json", "updateDefinition": json.dumps({"capabilities": capability_string(current | needed)})},
            timeout=60,
        )
        if not result.get("success"):
            raise RuntimeError(f"Could not update service capabilities for {service_url}: {result}")

    if layer_index is None:
        return
    layer_info = agol_request(
        "GET",
        f"{service_url}/{layer_index}",
        context=f"Could not read layer capabilities for {service_url}/{layer_index}",
        token=token,
        params={"f": "json"},
    )
    layer_current = capability_set(layer_info.get("capabilities", ""))
    if needed.issubset(layer_current):
        return
    result = agol_request(
        "POST",
        f"{admin_url}/{layer_index}/updateDefinition",
        context=f"Could not update layer capabilities for {service_url}/{layer_index}",
        token=token,
        data={"f": "json", "updateDefinition": json.dumps({"capabilities": capability_string(layer_current | needed)})},
        timeout=60,
    )
    if not result.get("success"):
        raise RuntimeError(f"Could not update layer capabilities for {service_url}/{layer_index}: {result}")


def truncate_layer(service_url: str, layer_index: int, token: str) -> None:
    result = agol_request(
        "POST",
        f"{admin_service_url(service_url)}/{layer_index}/truncate",
        context=f"truncate failed for {service_url}/{layer_index}",
        token=token,
        data={"f": "json", "attachmentOnly": "false", "async": "false"},
        timeout=300,
    )
    if not result.get("success", True):
        raise RuntimeError(f"truncate did not succeed for {service_url}/{layer_index}: {result}")


def chunks(values: list[Any], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def delete_layer_features_by_object_id(service_url: str, layer_index: int, token: str, batch_size: int = 5000) -> int:
    payload = agol_request(
        "GET",
        f"{service_url}/{layer_index}/query",
        context=f"Could not query object IDs for {service_url}/{layer_index}",
        token=token,
        params={"f": "json", "where": "1=1", "returnIdsOnly": "true"},
        timeout=120,
    )
    object_ids = payload.get("objectIds") or []
    deleted = 0
    for batch in chunks(object_ids, batch_size):
        result = agol_request(
            "POST",
            f"{service_url}/{layer_index}/deleteFeatures",
            context=f"deleteFeatures failed for {service_url}/{layer_index}",
            token=token,
            data={"f": "json", "objectIds": ",".join(str(value) for value in batch)},
            timeout=300,
        )
        failures = [item for item in result.get("deleteResults", []) if not item.get("success")]
        if failures:
            raise RuntimeError(f"deleteFeatures had failed deletes: {failures[:3]}")
        deleted += len(batch)
    return deleted


def clear_layer(service_url: str, layer_index: int, token: str) -> None:
    try:
        truncate_layer(service_url, layer_index, token)
    except Exception:
        delete_layer_features_by_object_id(service_url, layer_index, token)


def sanitize_field_name(raw_name: str, used: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_").lower()
    if not name:
        name = "field"
    if name[0].isdigit():
        name = f"f_{name}"
    if name in RESERVED_FIELD_NAMES:
        name = f"{name}_src"
    name = name[:60]
    base = name
    suffix = 2
    while name in used:
        tail = f"_{suffix}"
        name = f"{base[:60 - len(tail)]}{tail}"
        suffix += 1
    used.add(name)
    return name


def load_geojson(path: Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("type") == "FeatureCollection":
        features = obj.get("features") or []
    elif obj.get("type") == "Feature":
        features = [obj]
    else:
        raise ValueError(f"Expected FeatureCollection or Feature: {path}")
    if not features:
        raise ValueError(f"GeoJSON contains no features: {path}")
    return features


def esri_geometry_type(geojson_type: str) -> str:
    mapping = {
        "Point": "esriGeometryPoint",
        "MultiPoint": "esriGeometryMultipoint",
        "LineString": "esriGeometryPolyline",
        "MultiLineString": "esriGeometryPolyline",
        "Polygon": "esriGeometryPolygon",
        "MultiPolygon": "esriGeometryPolygon",
    }
    if geojson_type not in mapping:
        raise ValueError(f"Unsupported GeoJSON geometry type: {geojson_type}")
    return mapping[geojson_type]


def xy(coord: list[float]) -> list[float]:
    return [coord[0], coord[1]]


def geojson_geometry_to_esri(geometry: dict[str, Any]) -> dict[str, Any]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    spatial_reference = {"wkid": 4326}
    if geom_type == "Point":
        return {"x": coords[0], "y": coords[1], "spatialReference": spatial_reference}
    if geom_type == "MultiPoint":
        return {"points": [xy(coord) for coord in coords], "spatialReference": spatial_reference}
    if geom_type == "LineString":
        return {"paths": [[xy(coord) for coord in coords]], "spatialReference": spatial_reference}
    if geom_type == "MultiLineString":
        return {"paths": [[xy(coord) for coord in path] for path in coords], "spatialReference": spatial_reference}
    if geom_type == "Polygon":
        return {"rings": [[xy(coord) for coord in ring] for ring in coords], "spatialReference": spatial_reference}
    if geom_type == "MultiPolygon":
        rings = []
        for polygon in coords:
            rings.extend([[xy(coord) for coord in ring] for ring in polygon])
        return {"rings": rings, "spatialReference": spatial_reference}
    raise ValueError(f"Unsupported GeoJSON geometry type: {geom_type}")


def infer_field_type(values: list[Any]) -> tuple[str, int | None]:
    non_null = [value for value in values if value is not None and value != ""]
    if not non_null:
        return "esriFieldTypeString", 255
    if all(isinstance(value, bool) for value in non_null):
        return "esriFieldTypeSmallInteger", None
    if all(isinstance(value, int) and not isinstance(value, bool) for value in non_null):
        return "esriFieldTypeInteger", None
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in non_null):
        return "esriFieldTypeDouble", None
    max_len = max(len(str(value)) for value in non_null)
    return "esriFieldTypeString", min(max(255, max_len), 4000)


def prepare_geojson_features(features: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    geometry_types = {feature.get("geometry", {}).get("type") for feature in features if feature.get("geometry")}
    if not geometry_types:
        raise ValueError("No feature has geometry.")
    esri_types = {esri_geometry_type(value) for value in geometry_types}
    if len(esri_types) != 1:
        raise ValueError(f"Mixed geometry families are not supported in one layer: {sorted(geometry_types)}")
    geometry_type = esri_types.pop()

    original_names = []
    for feature in features:
        for key in (feature.get("properties") or {}).keys():
            if key not in original_names:
                original_names.append(key)

    used_names: set[str] = set()
    field_map = {name: sanitize_field_name(name, used_names) for name in original_names}
    values_by_field = {name: [] for name in original_names}
    for feature in features:
        props = feature.get("properties") or {}
        for name in original_names:
            values_by_field[name].append(props.get(name))

    fields = []
    field_lengths = {}
    for original_name in original_names:
        field_type, length = infer_field_type(values_by_field[original_name])
        field = {
            "name": field_map[original_name],
            "type": field_type,
            "alias": original_name[:255],
            "nullable": True,
        }
        if length:
            field["length"] = length
            field_lengths[field_map[original_name]] = length
        fields.append(field)

    esri_features = []
    for feature in features:
        props = feature.get("properties") or {}
        attrs = {}
        for original_name, esri_name in field_map.items():
            value = props.get(original_name)
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, str) and esri_name in field_lengths:
                value = value[: field_lengths[esri_name]]
            attrs[esri_name] = value
        esri_features.append(
            {
                "geometry": geojson_geometry_to_esri(feature["geometry"]),
                "attributes": attrs,
            }
        )

    return geometry_type, fields, {"features": esri_features}


def add_layer_to_service(service_url: str, token: str, layer_name: str, geometry_type: str, fields: list[dict[str, Any]]) -> None:
    admin_url = admin_service_url(service_url)
    layer_fields = [
        {
            "name": "OBJECTID",
            "type": "esriFieldTypeOID",
            "alias": "OBJECTID",
            "nullable": False,
            "editable": False,
        },
        *fields,
    ]
    layer_def = {
        "layers": [
            {
                "name": layer_name,
                "type": "Feature Layer",
                "geometryType": geometry_type,
                "objectIdField": "OBJECTID",
                "fields": layer_fields,
                "capabilities": "Query,Editing,Create,Update,Delete,Extract",
            }
        ]
    }
    result = agol_request(
        "POST",
        f"{admin_url}/addToDefinition",
        context=f"addToDefinition failed for {service_url}",
        token=token,
        data={"f": "json", "addToDefinition": json.dumps(layer_def)},
        timeout=60,
    )
    if not result.get("success"):
        raise RuntimeError(f"addToDefinition did not succeed: {result}")


def append_features(service_url: str, layer_index: int, token: str, features: list[dict[str, Any]], batch_size: int) -> int:
    total_added = 0
    for batch_number, batch in enumerate(chunks(features, batch_size), start=1):
        result = agol_request(
            "POST",
            f"{service_url}/{layer_index}/applyEdits",
            context=f"applyEdits failed for batch {batch_number}",
            token=token,
            data={"f": "json", "adds": json.dumps(batch)},
            timeout=300,
        )
        add_results = result.get("addResults") or []
        failures = [item for item in add_results if not item.get("success")]
        if failures:
            raise RuntimeError(f"applyEdits batch {batch_number} had failed adds: {failures[:3]}")
        added = sum(1 for item in add_results if item.get("success"))
        total_added += added
        print(f"  Batch {batch_number}: added {added:,} features")
    return total_added


def update_item_metadata(portal: str, username: str, token: str, item_id: str, layer: dict[str, Any], default_tags: list[str]) -> None:
    result = agol_request(
        "POST",
        f"{portal}/sharing/rest/content/users/{username}/items/{item_id}/update",
        context=f"Metadata update failed for {item_id}",
        token=token,
        data={"f": "json", **target_metadata(layer, default_tags)},
        timeout=60,
    )
    if not result.get("success"):
        raise RuntimeError(f"Metadata update did not succeed for {item_id}: {result}")


def share_item(portal: str, username: str, token: str, item_id: str, share: dict[str, Any]) -> None:
    group_ids = [str(value).strip() for value in share.get("group_ids", []) if str(value).strip()]
    if not group_ids and not share.get("org") and not share.get("everyone"):
        return
    result = agol_request(
        "POST",
        f"{portal}/sharing/rest/content/users/{username}/items/{item_id}/share",
        context=f"Item share failed for {item_id}",
        token=token,
        data={
            "f": "json",
            "everyone": str(bool(share.get("everyone"))).lower(),
            "org": str(bool(share.get("org"))).lower(),
            "groups": ",".join(group_ids),
            "confirmItemControl": "true",
        },
        timeout=60,
    )
    if not result.get("itemId") and not result.get("notSharedWith"):
        raise RuntimeError(f"Unexpected share response for {item_id}: {result}")
    not_shared = result.get("notSharedWith") or []
    if not_shared:
        raise RuntimeError(f"Item {item_id} was not shared with these groups: {not_shared}")


def publish_layer(
    portal: str,
    username: str,
    token: str,
    identity: dict[str, Any],
    layer: dict[str, Any],
    default_tags: list[str],
    geometry_type: str,
    fields: list[dict[str, Any]],
    esri_features: list[dict[str, Any]],
    batch_size: int,
    max_record_count: int,
) -> tuple[str, int]:
    item_id = layer.get("item_id") or ""
    service_url = ""
    created = False

    if item_id:
        item = validate_item_permission(portal, token, username, identity, item_id)
        service_url = item.get("url") or ""
    else:
        existing = find_feature_service_by_title(portal, token, username, layer["title"])
        if existing:
            item_id = existing["id"]
            validate_item_permission(portal, token, username, identity, item_id)
            service_url = existing.get("url") or get_item(portal, token, item_id).get("url") or ""
            print(f"  Reusing existing item with matching title: {item_id}")
        else:
            item_id, service_url = create_feature_service(portal, username, token, layer, default_tags, max_record_count)
            created = True
            print(f"  Created item_id={item_id}. Add this item_id to the layer registry for stable updates.")

    if not service_url:
        raise RuntimeError(f"Feature service item has no service URL: {item_id}")

    info = service_info(service_url, token)
    layers = info.get("layers") or []
    if created or not layers:
        add_layer_to_service(service_url, token, layer.get("layer_name") or "features", geometry_type, fields)
        layers = service_info(service_url, token).get("layers") or []
    if not layers:
        raise RuntimeError(f"Feature service has no layers after addToDefinition: {service_url}")

    layer_index = layers[0].get("id", 0)
    ensure_service_capabilities(service_url, token, layer_index)
    clear_layer(service_url, layer_index, token)
    added = append_features(service_url, layer_index, token, esri_features, batch_size)
    update_item_metadata(portal, username, token, item_id, layer, default_tags)
    share_item(portal, username, token, item_id, layer.get("share") or {})
    return item_id, added


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_layer_keys(registry: dict[str, Any], requested: str) -> list[str]:
    layers = registry.get("layers") or {}
    if requested == "all":
        return list(layers.keys())
    keys = [key.strip() for key in requested.split(",") if key.strip()]
    unknown = [key for key in keys if key not in layers]
    if unknown:
        raise ValueError(f"Unknown layer key(s): {unknown}. Available: {sorted(layers)}")
    return keys


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--layer", default="piu_project_locations_adm3", help="Layer key, comma-separated keys, or all")
    parser.add_argument("--publish", action="store_true", help="Actually write to ArcGIS Online. Default is dry-run.")
    parser.add_argument("--list-groups", action="store_true", help="List groups available to the AGOL user and exit.")
    parser.add_argument("--token-expiration", type=int, default=120)
    parser.add_argument(
        "--auth",
        choices=["auto", "browser", "client", "password"],
        default=os.getenv("AGOL_AUTH_MODE", "auto"),
        help="Authentication mode. auto prefers client credentials, then username/password. browser opens a user sign-in prompt.",
    )
    args = parser.parse_args()

    registry = load_registry(args.registry)
    root = args.registry.resolve().parents[2] if len(args.registry.resolve().parents) >= 3 else Path.cwd()
    portal = os.getenv("AGOL_PORTAL_URL") or registry.get("portal") or "https://www.arcgis.com"
    os.environ.setdefault("AGOL_REFERER", portal)
    defaults = registry.get("defaults") or {}
    default_tags = defaults.get("tags") or []
    batch_size = int(defaults.get("batch_size") or 100)
    max_record_count = int(defaults.get("max_record_count") or 10000)

    token = ""
    identity: dict[str, Any] = {}
    client_id = os.getenv("AGOL_CLIENT_ID", "")
    client_secret = os.getenv("AGOL_CLIENT_SECRET", "")
    username = os.getenv("AGOL_USERNAME", "")
    password = os.getenv("AGOL_PASSWORD", "")
    redirect_uri = os.getenv("AGOL_REDIRECT_URI", "http://127.0.0.1:8765/callback")
    has_client_credentials = configured_secret(client_id) and configured_secret(client_secret)
    has_user_credentials = configured_secret(username) and configured_secret(password)
    has_browser_credentials = configured_secret(client_id)
    if args.publish or args.list_groups or has_client_credentials or has_user_credentials or args.auth == "browser":
        if args.auth == "browser":
            if not has_browser_credentials:
                raise RuntimeError("Set AGOL_CLIENT_ID before using --auth browser.")
            token = get_agol_browser_token(portal, client_id, redirect_uri)
            identity = validate_agol_user(portal, token)
            username = identity["username"]
            print(f"AGOL browser user validated: {username} ({identity.get('role')})")
        elif args.auth == "client":
            if not has_client_credentials:
                raise RuntimeError("Set AGOL_CLIENT_ID and AGOL_CLIENT_SECRET before using --auth client.")
            token = get_agol_client_token(client_id, client_secret)
            identity = validate_agol_token_owner(portal, token)
            username = identity["username"]
            print(f"AGOL OAuth app token validated: {username} ({identity.get('role')})")
        elif args.auth == "password":
            if not has_user_credentials:
                raise RuntimeError("Set AGOL_USERNAME and AGOL_PASSWORD before using --auth password.")
            token = get_agol_user_token(portal, username, password, args.token_expiration)
            identity = validate_agol_user(portal, token)
            username = identity["username"]
            print(f"AGOL user validated: {username} ({identity.get('role')})")
        elif has_client_credentials:
            token = get_agol_client_token(client_id, client_secret)
            identity = validate_agol_token_owner(portal, token)
            username = identity["username"]
            print(f"AGOL OAuth app token validated: {username} ({identity.get('role')})")
        elif has_user_credentials:
            token = get_agol_user_token(portal, username, password, args.token_expiration)
            identity = validate_agol_user(portal, token)
            username = identity["username"]
            print(f"AGOL user validated: {username} ({identity.get('role')})")
        else:
            raise RuntimeError(
                "Set AGOL_CLIENT_ID and AGOL_CLIENT_SECRET, AGOL_USERNAME and AGOL_PASSWORD, "
                "or use --auth browser with AGOL_CLIENT_ID, "
                "before using --publish or --list-groups."
            )
    elif not args.publish:
        print("Dry run without AGOL credentials: validating local GeoJSON only.")

    if args.list_groups:
        if not token:
            raise RuntimeError("Set AGOL_USERNAME and AGOL_PASSWORD before using --list-groups.")
        groups = list_user_groups(portal, token, username)
        print("\nAvailable groups:")
        for group in sorted(groups, key=lambda item: (item.get("title") or "").lower()):
            print(f"  {group.get('id')} | {group.get('title')} | owner={group.get('owner')} | access={group.get('access')}")
        return

    for key in selected_layer_keys(registry, args.layer):
        layer = registry["layers"][key]
        path = Path(layer["path"])
        if not path.is_absolute():
            path = root / path
        features = load_geojson(path)
        geometry_type, fields, prepared = prepare_geojson_features(features)
        esri_features = prepared["features"]

        print(f"\n=== {key} ===")
        print(f"  title:         {layer['title']}")
        print(f"  path:          {path}")
        print(f"  features:      {len(esri_features):,}")
        print(f"  geometry_type: {geometry_type}")
        print(f"  fields:        {len(fields)}")
        share = layer.get("share") or {}
        print(f"  share_org:     {bool(share.get('org'))}")
        print(f"  share_public:  {bool(share.get('everyone'))}")
        print(f"  share_groups:  {', '.join(share.get('group_ids') or []) or '(none)'}")

        if not args.publish:
            if token:
                item_id = layer.get("item_id") or ""
                if item_id:
                    item = validate_item_permission(portal, token, username, identity, item_id)
                    print(f"  AGOL item:     OK ({item_id}, owner={item.get('owner')})")
                else:
                    existing = find_feature_service_by_title(portal, token, username, layer["title"])
                    if existing:
                        print(f"  AGOL item:     matching existing item: {existing['id']}")
                    else:
                        print("  AGOL item:     no matching item; publish run will create one")
            continue

        item_id, added = publish_layer(
            portal,
            username,
            token,
            identity,
            layer,
            default_tags,
            geometry_type,
            fields,
            esri_features,
            batch_size,
            max_record_count,
        )
        print(f"  published:     item_id={item_id}, features={added:,}")


if __name__ == "__main__":
    main()
