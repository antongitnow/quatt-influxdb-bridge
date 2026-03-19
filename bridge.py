#!/usr/bin/env python3
"""
Quatt CIC → InfluxDB 3 bridge.
Polls /beta/feed/data.json and writes each section as a separate InfluxDB measurement.
"""

import os
import sys
import time
import json
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config from environment ────────────────────────────────────────────────────
CIC_IP         = os.environ["CIC_IP"]
INFLUXDB_IP    = os.environ["INFLUXDB_IP"]
INFLUXDB_PORT  = os.environ.get("INFLUXDB_PORT", "8086")
INFLUXDB_DB    = os.environ.get("INFLUXDB_DB", "quatt")
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "10"))

CIC_URL      = f"http://{CIC_IP}:8080/beta/feed/data.json"
INFLUX_WRITE = f"http://{INFLUXDB_IP}:{INFLUXDB_PORT}/api/v2/write"

# Sections to skip entirely
SKIP_SECTIONS = {"time", "thread"}

# String fields to promote to tags (appended to measurement name via tag set)
STRING_FIELDS = {"hostName"}


def flatten(obj: dict, prefix: str = "") -> tuple[dict, dict]:
    """
    Recursively flatten nested JSON.
    Returns (fields, tags) where tags are string values from STRING_FIELDS.
    """
    fields: dict = {}
    tags: dict = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if v is None:
            continue
        if isinstance(v, dict):
            sub_fields, sub_tags = flatten(v, key)
            fields.update(sub_fields)
            tags.update(sub_tags)
        elif isinstance(v, bool):
            fields[key] = v
        elif isinstance(v, (int, float)):
            fields[key] = v
        elif isinstance(v, str) and v:
            leaf = key.split(".")[-1]
            if leaf in STRING_FIELDS:
                tags[leaf] = v
            # Other strings are silently dropped (not useful as fields)
    return fields, tags


def escape_tag(value: str) -> str:
    return value.replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def field_to_lp(k: str, v) -> str:
    safe = k.replace(".", "_").replace(" ", "_")
    if isinstance(v, bool):
        return f"{safe}={'true' if v else 'false'}"
    if isinstance(v, int):
        return f"{safe}={v}i"
    if isinstance(v, float):
        return f"{safe}={v}"
    return f'{safe}="{v}"'


def to_line_protocol(
    measurement: str,
    fields: dict,
    tags: dict,
    timestamp_ns: int,
) -> str | None:
    if not fields:
        return None

    tag_str = ""
    if tags:
        tag_parts = ",".join(
            f"{escape_tag(k)}={escape_tag(v)}" for k, v in sorted(tags.items())
        )
        tag_str = f",{tag_parts}"

    field_str = ",".join(field_to_lp(k, v) for k, v in fields.items())
    return f"{measurement}{tag_str} {field_str} {timestamp_ns}"


def fetch_cic() -> dict:
    resp = requests.get(CIC_URL, timeout=8)
    resp.raise_for_status()
    return resp.json()


def write_to_influx(lines: list[str]) -> None:
    payload = "\n".join(lines)
    headers = {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type":  "text/plain; charset=utf-8",
    }
    params = {
        "bucket":    INFLUXDB_DB,
        "precision": "ns",
    }
    resp = requests.post(
        INFLUX_WRITE,
        headers=headers,
        params=params,
        data=payload.encode("utf-8"),
        timeout=10,
    )
    resp.raise_for_status()


def build_lines(data: dict) -> list[str]:
    """
    Split the CIC payload into per-section InfluxDB measurements.
    Uses time.ts (milliseconds) from the payload as the timestamp when available,
    otherwise falls back to wall clock.
    """
    # Extract timestamp from payload (ms → ns)
    ts_ns: int
    time_section = data.get("time")
    if isinstance(time_section, dict) and isinstance(time_section.get("ts"), (int, float)):
        ts_ns = int(time_section["ts"]) * 1_000_000  # ms → ns
    else:
        ts_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    lines: list[str] = []

    for section, value in data.items():
        if section in SKIP_SECTIONS:
            continue
        if not isinstance(value, dict):
            continue  # skip null sections

        measurement = f"quatt_{section}"
        fields, tags = flatten(value)

        if not fields:
            continue

        line = to_line_protocol(measurement, fields, tags, ts_ns)
        if line:
            lines.append(line)

    return lines


def run() -> None:
    log.info("Quatt CIC → InfluxDB bridge starting")
    log.info("  CIC URL : %s", CIC_URL)
    log.info("  InfluxDB: %s:%s  bucket=%s", INFLUXDB_IP, INFLUXDB_PORT, INFLUXDB_DB)
    log.info("  Interval: %ds", POLL_INTERVAL)

    consecutive_errors = 0

    while True:
        try:
            data = fetch_cic()
            lines = build_lines(data)

            if lines:
                write_to_influx(lines)
                log.info("Wrote %d measurements", len(lines))
            else:
                log.warning("No data extracted from CIC response")

            consecutive_errors = 0

        except requests.exceptions.ConnectionError as e:
            consecutive_errors += 1
            log.error("Connection error (%d): %s", consecutive_errors, e)
        except requests.exceptions.HTTPError as e:
            consecutive_errors += 1
            log.error("HTTP error (%d): %s", consecutive_errors, e)
        except json.JSONDecodeError as e:
            consecutive_errors += 1
            log.error("Invalid JSON from CIC (%d): %s", consecutive_errors, e)
        except Exception as e:
            consecutive_errors += 1
            log.exception("Unexpected error (%d): %s", consecutive_errors, e)

        if consecutive_errors >= 10:
            log.critical("10 consecutive errors – sleeping 60s before retrying")
            time.sleep(60)
            consecutive_errors = 0
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
