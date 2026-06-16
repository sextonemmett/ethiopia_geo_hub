#!/usr/bin/env python3
"""Download project submission data from KoboToolbox.

Credentials are read from environment variables:
  KOBO_API_TOKEN

A local .env file is also loaded if present. Keep .env untracked.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ASSET_ID = "aKX2WQUSmQ87QrCUxJzWCp"
DEFAULT_BASE_URL = "https://kf.kobotoolbox.org/api/v2"
DEFAULT_OUTPUT = Path("data/projects/kobo_project_data.json")


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


def build_data_url(base_url: str, asset_id: str) -> str:
    return f"{base_url.rstrip('/')}/assets/{asset_id}/data/"


def fetch_kobo_data(url: str, token: str, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Token {token}",
            "User-Agent": "ethiopia-geo-hub-kobo-downloader/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                prefix = response.read(200).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Kobo returned a non-JSON response. content-type={content_type!r}; prefix={prefix!r}"
                )
            payload = json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"Kobo request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Kobo request failed: {exc.reason}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object from Kobo, got {type(payload).__name__}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as file:
        temp_path = Path(file.name)
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temp_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-id",
        default=DEFAULT_ASSET_ID,
        help=f"Kobo asset id to download. Default: {DEFAULT_ASSET_ID}",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Kobo API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout in seconds. Default: 60",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()

    token = os.getenv("KOBO_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set KOBO_API_TOKEN in the environment or in .env before running this script.")

    url = build_data_url(args.base_url, args.asset_id)
    payload = fetch_kobo_data(url, token, args.timeout)
    write_json_atomic(args.output, payload)

    results = payload.get("results")
    result_count = len(results) if isinstance(results, list) else None
    total_count = payload.get("count")
    summary = f"Wrote {args.output}"
    if total_count is not None:
        summary += f" with count={total_count}"
    if result_count is not None:
        summary += f", results={result_count}"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
