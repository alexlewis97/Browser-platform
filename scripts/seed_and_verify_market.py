#!/usr/bin/env python3
"""Seed marketplace listings and verify JSON publish -> list -> package download.

Reads BROWSER_PLATFORM_USER_TOKEN from the environment. Never prints the token.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ORIGIN = os.environ.get("MARKET_ORIGIN", "https://browser-platform-web.vercel.app").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
SEEDS = [
    {
        "path": ROOT / "examples" / "attendance.workflow.json",
        "type": "workflow-use",
    },
    {
        "path": ROOT
        / "data"
        / "live_from_task"
        / "workflows"
        / "f99ba8f7fc1a477dbab33bc99e0c164f.workflow.json",
        "type": "from-task",
    },
]


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN") or os.environ.get(
        "BROWSER_PLATFORM_ACCESS_TOKEN", ""
    )
    if not value:
        sys.exit("BROWSER_PLATFORM_USER_TOKEN is not set")
    return value


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-Browser-Platform-Token": token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(ORIGIN + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read()
            ctype = res.headers.get("content-type") or ""
            sha = res.headers.get("x-package-sha256")
            if "application/json" in ctype:
                parsed = json.loads(raw.decode("utf-8") or "{}")
                return res.status, parsed
            return res.status, {"_bytes": raw, "sha": sha, "status": res.status}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            parsed = {"error": raw.decode("utf-8", "replace")[:300]}
        return err.code, parsed


def slug(name: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:60] or "listing"


def list_listings() -> list[dict]:
    status, data = request("GET", "/api/market")
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"list failed: {status} {data if isinstance(data, dict) else 'binary'}")
    return list(data.get("listings") or [])


def seed() -> list[str]:
    existing = {item.get("name"): item for item in list_listings()}
    done: list[str] = []
    for spec in SEEDS:
        workflow = json.loads(spec["path"].read_text())
        name = workflow["name"]
        if name in existing:
            listing = existing[name]
            done.append(f"already-present {name} id={listing.get('id')} v{listing.get('version')} sha256={listing.get('sha256')}")
            continue
        payload = {
            "id": slug(name),
            "name": name,
            "description": workflow.get("description") or "",
            "type": spec["type"],
            "workflow": workflow,
        }
        status, data = request("POST", "/api/market", payload)
        if status not in (200, 201) or not isinstance(data, dict) or "listing" not in data:
            raise SystemExit(f"publish {name} failed: {status} {data}")
        listing = data["listing"]
        done.append(f"published {listing['name']} id={listing['id']} v{listing['version']} sha256={listing['sha256']} size={listing['size']}")
    return done


def verify_package(listing: dict) -> str:
    status, data = request("GET", f"/api/market/{listing['id']}/package")
    if status != 200 or not isinstance(data, dict) or "_bytes" not in data:
        raise SystemExit(f"package {listing.get('id')} failed: {status}")
    raw: bytes = data["_bytes"]
    digest = hashlib.sha256(raw).hexdigest()
    if listing.get("sha256") and listing["sha256"] != digest:
        raise SystemExit(f"sha256 mismatch for {listing['id']}")
    if data.get("sha") and data["sha"] != digest:
        raise SystemExit(f"header sha256 mismatch for {listing['id']}")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if "workflow.json" not in names:
            raise SystemExit(f"package {listing['id']} missing workflow.json: {names}")
        workflow = json.loads(zf.read("workflow.json"))
    if workflow.get("name") != listing["name"]:
        raise SystemExit(f"package name mismatch: {workflow.get('name')} vs {listing['name']}")
    return f"package-ok {listing['name']} v{listing['version']} files={names} sha256={digest}"


def e2e_temp() -> list[str]:
    name = f"e2e_json_{int(time.time())}"
    workflow = {
        "name": name,
        "description": "temporary publish JSON probe",
        "version": "1",
        "input_schema": [{"name": "term", "type": "string", "required": True}],
        "steps": [
            {"type": "navigation", "url": "https://example.com/?q={term}"},
            {"type": "extract", "selector": "h1", "name": "heading"},
        ],
    }
    status, data = request("POST", "/api/market", {"name": name, "type": "workflow-use", "workflow": workflow})
    if status not in (200, 201) or not isinstance(data, dict) or "listing" not in data:
        raise SystemExit(f"temp publish failed: {status} {data}")
    listing = data["listing"]
    notes = [f"temp-published {listing['name']} id={listing['id']} v{listing['version']}"]
    notes.append(verify_package(listing))
    status2, data2 = request("POST", "/api/market", {"id": listing["id"], "name": name, "type": "workflow-use", "workflow": workflow})
    if status2 not in (200, 201) or not isinstance(data2, dict) or "listing" not in data2:
        raise SystemExit(f"temp update failed: {status2} {data2}")
    updated = data2["listing"]
    if updated["version"] in (listing["version"], "1"):
        raise SystemExit(f"version did not bump: {updated['version']}")
    notes.append(f"temp-updated {updated['name']} v{updated['version']}")
    del_status, _ = request("DELETE", f"/api/market/{listing['id']}")
    if del_status not in (200, 204):
        raise SystemExit(f"temp delete failed: {del_status}")
    names = {item.get("name") for item in list_listings()}
    if name in names:
        raise SystemExit("temp listing still present after delete")
    notes.append(f"temp-deleted {listing['id']}")
    return notes


def main() -> None:
    print(f"origin={ORIGIN}")
    for line in seed():
        print(line)
    listings = list_listings()
    names = {item.get("name") for item in listings}
    for expected in ("attendance_mark", "wikipedia_search_from_task"):
        if expected not in names:
            raise SystemExit(f"missing seed listing {expected}; have {sorted(names)}")
    for item in listings:
        if item.get("name") in ("attendance_mark", "wikipedia_search_from_task"):
            print(verify_package(item))
    print(f"listings={len(listings)}")
    for line in e2e_temp():
        print(line)
    print("ok")


if __name__ == "__main__":
    main()
