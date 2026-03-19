#!/usr/bin/env python3
"""
Quatt CIC → InfluxDB bridge.
Supports InfluxDB v2 (bucket/org, Token auth) and v3 (database, Bearer auth).
Polls /beta/feed/data.json and writes each section as a separate measurement.
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
CIC_IP           = os.environ["CIC_IP"]
INFLUXDB_IP      = os.environ["INFLUXDB_IP"]
INFLUXDB_PORT    = os.environ.get("INFLUXDB_PORT", "8086")
INFLUXDB_VERSION = os.environ.get("INFLUXDB_VERSION", "3")   # "2" or "3"
INFLUXDB_ORG     = os.environ.get("INFLUXDB_ORG", "")        # required for v2
INFLUXDB_DB      = os.environ.get("INFLUXDB_DB", "quatt")    # bucket (v2) or database (v3)
INFLUXDB_TOKEN   = os.environ["INFLUXDB_TOKEN"]
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "10"))

if INFLUXDB_VERSION not in ("2", "3"):
    log.error("INFLUXDB_VERSION must be '2' or '3', got: %s", INFLUXDB_VERSION)
    sys.exit(1)

if INFLUXDB_VERSION == "2" and not INFLUXDB_ORG:
    log.error("INFLUXDB_ORG is required when INFLUXDB_VERSION=2")
    sys.exit(1)

CIC_URL     = f"http://{CIC_IP}:8080/beta/feed/data.json"
INFLUX_BASE = f"http://{INFLUXDB_IP}:{INFLUXDB_PORT}"

# Sections to skip entirely
SKIP_SECTIONS = {"time", "thread"}

# String leaf keys to promote to tags instead of fields
STRING_FIELDS = {"hostName"}


# ── Line-protocol helpers ──────────────────────────────────────────────────────

def flatten(obj: dict, prefix: str = "") -> tuple[dict, dict]:
    """Recursively flatten nested JSON into (fields, tags)."""
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
            # Other strings are dropped – not useful as InfluxDB fields
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


def to_line_protocol(measurement: str, fields: dict, tags: dict, timestamp_ns: int) -> str | None:
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


# ── CIC fetcher ────────────────────────────────────────────────────────────────

def fetch_cic() -> dict:
    resp = requests.get(CIC_URL, timeout=8)
    resp.raise_for_status()
    return resp.json()


def build_lines(data: dict) -> list[str]:
    """Split the CIC payload into per-section InfluxDB measurements."""
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
            continue
        fields, tags = flatten(value)
        if not fields:
            continue
        line = to_line_protocol(f"quatt_{section}", fields, tags, ts_ns)
        if line:
            lines.append(line)
    return lines


# ── InfluxDB v2 ────────────────────────────────────────────────────────────────

def _v2_headers(content_type: str = "text/plain; charset=utf-8") -> dict:
    return {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type":  content_type,
    }


def ensure_database_v2() -> None:
    """Create the InfluxDB v2 bucket if it does not already exist."""
    # 1. Resolve org → orgID
    resp = requests.get(
        f"{INFLUX_BASE}/api/v2/orgs",
        headers=_v2_headers("application/json"),
        params={"org": INFLUXDB_ORG},
        timeout=10,
    )
    resp.raise_for_status()
    orgs = resp.json().get("orgs", [])
    if not orgs:
        raise RuntimeError(f"Organisation '{INFLUXDB_ORG}' not found in InfluxDB v2")
    org_id = orgs[0]["id"]

    # 2. Create bucket (422 = already exists)
    resp = requests.post(
        f"{INFLUX_BASE}/api/v2/buckets",
        headers=_v2_headers("application/json"),
        json={"name": INFLUXDB_DB, "orgID": org_id},
        timeout=10,
    )
    if resp.status_code in (200, 201, 422):
        log.info("InfluxDB v2 bucket '%s' ready (org: %s).", INFLUXDB_DB, INFLUXDB_ORG)
    else:
        resp.raise_for_status()


def write_to_influx_v2(lines: list[str]) -> None:
    payload = "\n".join(lines)
    resp = requests.post(
        f"{INFLUX_BASE}/api/v2/write",
        headers=_v2_headers(),
        params={"org": INFLUXDB_ORG, "bucket": INFLUXDB_DB, "precision": "ns"},
        data=payload.encode("utf-8"),
        timeout=10,
    )
    resp.raise_for_status()


# ── InfluxDB v3 ────────────────────────────────────────────────────────────────

def _v3_headers(content_type: str = "text/plain; charset=utf-8") -> dict:
    return {
        "Authorization": f"Bearer {INFLUXDB_TOKEN}",
        "Content-Type":  content_type,
    }


def ensure_database_v3() -> None:
    """Create the InfluxDB v3 database if it does not already exist."""
    resp = requests.post(
        f"{INFLUX_BASE}/api/v3/configure/database",
        headers=_v3_headers("application/json"),
        json={"db": INFLUXDB_DB},
        timeout=10,
    )
    if resp.status_code in (200, 201, 409):  # 409 = already exists
        log.info("InfluxDB v3 database '%s' ready.", INFLUXDB_DB)
    else:
        resp.raise_for_status()


def write_to_influx_v3(lines: list[str]) -> None:
    payload = "\n".join(lines)
    resp = requests.post(
        f"{INFLUX_BASE}/api/v3/write_lp",
        headers=_v3_headers(),
        params={"db": INFLUXDB_DB, "precision": "nanoseconds"},
        data=payload.encode("utf-8"),
        timeout=10,
    )
    resp.raise_for_status()


# ── Dispatch ───────────────────────────────────────────────────────────────────

def ensure_database() -> None:
    if INFLUXDB_VERSION == "2":
        ensure_database_v2()
    else:
        ensure_database_v3()


def write_to_influx(lines: list[str]) -> None:
    if INFLUXDB_VERSION == "2":
        write_to_influx_v2(lines)
    else:
        write_to_influx_v3(lines)


# ── Main loop ──────────────────────────────────────────────────────────────────

def run() -> None:
    if INFLUXDB_VERSION == "2":
        log.info("Quatt CIC → InfluxDB 2 bridge starting")
        log.info("  CIC URL  : %s", CIC_URL)
        log.info("  InfluxDB : %s:%s  org=%s  bucket=%s", INFLUXDB_IP, INFLUXDB_PORT, INFLUXDB_ORG, INFLUXDB_DB)
    else:
        log.info("Quatt CIC → InfluxDB 3 bridge starting")
        log.info("  CIC URL  : %s", CIC_URL)
        log.info("  InfluxDB : %s:%s  database=%s", INFLUXDB_IP, INFLUXDB_PORT, INFLUXDB_DB)
    log.info("  Interval : %ds", POLL_INTERVAL)

    ensure_database()

    consecutive_errors = 0

    while True:
        try:
            data  = fetch_cic()
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
