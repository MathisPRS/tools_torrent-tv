#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qb_restore_setlocation_from_radarrlist.py

Variant of qb_restore_setlocation_only.py that accepts input JSON in two forms:
 - old Radarr-like mapping: { "<key>": { "lastHash": "...", "folder": "...", ... }, ... }
 - new list form: [ { "radarr_id": 517, "title": "...", "folder": "...", "mkv_file": "...", "torrent_hash": "..." }, ... ]

Behaviour:
 - For torrents filtered by tag (ex: "restore"), change only the save_path (setLocation -> folder from input JSON) then recheck.
 - No rename, no category/genre change.
 - DRY_RUN = True by default; use --apply to actually perform actions.
 - The loader will map by torrent hash (uppercase).

Usage (dry-run):
  python3 qb_restore_setlocation_from_radarrlist.py -i radarr_export.json -t restore

Apply:
  python3 qb_restore_setlocation_from_radarrlist.py -i radarr_export.json -t restore --apply --yes
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import os
import requests

# =============================
# SAFETY SWITCH (DEFAULT)
# =============================
DRY_RUN = True  # Par défaut : aucune modification

# =============================
# DEFAULTS (modifiable via ENV or CLI)
# =============================
DEFAULT_QBT_HOST = os.environ.get("QBT_HOST", "http://127.0.0.1:8080")
DEFAULT_QBT_USER = os.environ.get("QBT_USER", "admin")
DEFAULT_QBT_PASS = os.environ.get("QBT_PASS", "adminadmin")

SLEEP_AFTER_SETLOCATION = 0.2
SLEEP_AFTER_RECHECK = 0.6
TIMEOUT = 15.0

OUT_PLAN = Path("qb_restore_plan.json")
OUT_RESULTS = Path("qb_restore_results_clean.json")

# ---------------- qBittorrent API helpers ----------------
def qb_login(s: requests.Session, host: str, user: str, password: str):
    url = host.rstrip("/") + "/api/v2/auth/login"
    try:
        r = s.post(url, data={"username": user, "password": password}, timeout=10)
    except requests.RequestException as e:
        raise RuntimeError(f"HTTP error during login: {e}")
    if r.status_code != 200 or "SID" not in s.cookies.get_dict():
        raise RuntimeError(f"qBittorrent login failed: HTTP {r.status_code} - {r.text[:200]}")

def qb_get_torrents(s: requests.Session, host: str, category: Optional[str]=None) -> List[Dict[str,Any]]:
    url = host.rstrip("/") + "/api/v2/torrents/info"
    params = {}
    if category:
        params["category"] = category
    r = s.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def qb_set_location(s: requests.Session, host: str, hashes, location: str):
    url = host.rstrip("/") + "/api/v2/torrents/setLocation"
    data = {"hashes": ",".join(hashes) if isinstance(hashes, (list,tuple)) else hashes, "location": location}
    r = s.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def qb_recheck(s: requests.Session, host: str, hashes):
    url = host.rstrip("/") + "/api/v2/torrents/recheck"
    data = {"hashes": ",".join(hashes) if isinstance(hashes,(list,tuple)) else hashes}
    r = s.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def qb_get_torrent_info(s: requests.Session, host: str, hash_: str) -> Optional[Dict[str,Any]]:
    url = host.rstrip("/") + "/api/v2/torrents/info"
    r = s.get(url, params={"hashes": hash_}, timeout=TIMEOUT)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None

# ---------------- helpers for mapping & selection ----------------
def load_input_json(p: Path) -> Dict[str, Dict[str,Any]]:
    """
    Load input JSON and return map hash_upper -> entry.

    Supports two input shapes:
      - mapping: { "<key>": { "lastHash": "...", "folder": "...", ... }, ... }
      - list: [ { "torrent_hash": "...", "folder": "...", ... }, ... ]
    """
    if not p.exists():
        raise FileNotFoundError(f"Input JSON not found: {p}")
    j = json.loads(p.read_text(encoding="utf-8"))

    by_hash: Dict[str, Dict[str,Any]] = {}

    if isinstance(j, dict):
        # old mapping form: iterate values and use lastHash if present
        for k, v in j.items():
            if not isinstance(v, dict):
                continue
            # check possible fields
            h = (v.get("lastHash") or v.get("torrent_hash") or v.get("torrentHash") or v.get("hash") or "").strip().upper()
            if h:
                by_hash[h] = v
    elif isinstance(j, list):
        # new list form: each element should contain torrent_hash or hash
        for v in j:
            if not isinstance(v, dict):
                continue
            h = (v.get("torrent_hash") or v.get("lastHash") or v.get("hash") or "").strip().upper()
            if h:
                by_hash[h] = v
    else:
        raise ValueError("Unsupported JSON structure: expected object or list at top level")

    return by_hash

def parse_tags(tag_str: Optional[str]) -> List[str]:
    if not tag_str:
        return []
    return [t.strip().lower() for t in tag_str.split(",") if t.strip()]

def dirname_of_path(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return str(Path(p).parent)

# ---------------- main ----------------
def main(argv=None):
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Set save_path (setLocation) from input JSON for torrents filtered by tag, then recheck. No rename.")
    parser.add_argument("-i","--input", type=Path, required=True, help="Input JSON (list or mapping).")
    parser.add_argument("-t","--tag", required=True, help="qBittorrent tag to filter (ex: restore)")
    parser.add_argument("-c","--category", help="Category filter (optional)")
    parser.add_argument("--host", default=DEFAULT_QBT_HOST, help="qBittorrent host (or set QBT_HOST env)")
    parser.add_argument("--user", default=DEFAULT_QBT_USER, help="qBittorrent user (or set QBT_USER env)")
    parser.add_argument("--passw", default=DEFAULT_QBT_PASS, help="qBittorrent password (or set QBT_PASS env)")
    parser.add_argument("--apply", action="store_true", help="If set, disable DRY_RUN and actually apply changes")
    parser.add_argument("--yes", action="store_true", help="Bypass interactive confirmation when applying")
    parser.add_argument("--verbose", action="store_true", help="Verbose logs to stderr")
    args = parser.parse_args(argv)

    # toggle DRY_RUN from CLI
    if args.apply:
        DRY_RUN = False

    if args.verbose:
        print(f"[VERB] DRY_RUN = {DRY_RUN}", file=sys.stderr)

    # load mapping JSON
    try:
        input_map = load_input_json(args.input)
    except Exception as e:
        print(f"[ERROR] Failed to load input JSON: {e}", file=sys.stderr)
        return 1
    if args.verbose:
        print(f"[VERB] Loaded {len(input_map)} mappings from {args.input}", file=sys.stderr)

    # login to qBittorrent
    s = requests.Session()
    try:
        qb_login(s, args.host, args.user, args.passw)
    except Exception as e:
        print(f"[ERROR] qBittorrent login failed: {e}", file=sys.stderr)
        return 2

    # fetch torrents (optionally filter by category at API level)
    try:
        all_torrents = qb_get_torrents(s, args.host, category=args.category)
    except Exception as e:
        print(f"[ERROR] Cannot fetch torrents: {e}", file=sys.stderr)
        return 3

    # filter by tag (client-side)
    selected = []
    for t in all_torrents:
        tags = parse_tags(t.get("tags"))
        if args.tag.lower() in tags:
            selected.append(t)

    # Print number of found torrents (single integer on stdout)
    print(len(selected))

    if args.verbose:
        print(f"[VERB] Found {len(selected)} torrents with tag '{args.tag}' (category: {args.category})", file=sys.stderr)

    # Build actions: match selected torrents with input_map via torrent hash
    actions = []
    for t in selected:
        h = (t.get("hash") or "").strip().upper()
        if not h:
            if args.verbose:
                print(f"[VERB] Torrent without hash? skipping: {t.get('name')}", file=sys.stderr)
            continue
        mapping = input_map.get(h)
        if not mapping:
            if args.verbose:
                print(f"[VERB] No mapping for hash {h} (torrent '{t.get('name')}')", file=sys.stderr)
            continue
        # Prefer mapping["folder"], fallback to dirname_of_path on common fields
        target_folder = mapping.get("folder") or mapping.get("Folder") or dirname_of_path(mapping.get("importedFilePath") or mapping.get("moviefile_path") or mapping.get("movieFile") or mapping.get("mkv_file"))
        # Defensive: if the mapping field points to a .mkv file, convert to parent folder
        if target_folder and target_folder.lower().endswith(".mkv"):
            target_folder = dirname_of_path(mapping.get("importedFilePath") or mapping.get("moviefile_path") or mapping.get("mkv_file"))

        actions.append({
            "hash": h,
            "name": t.get("name"),
            "save_path": t.get("save_path") or t.get("savePath") or "",
            "state": t.get("state"),
            "target_folder": target_folder,
            "mapping_raw": mapping
        })

    if args.verbose:
        print(f"[VERB] Actions to consider (mapped hashes): {len(actions)}", file=sys.stderr)

    if not actions:
        if args.verbose:
            print("[VERB] No mapped actions to perform. Exiting.", file=sys.stderr)
        return 0

    # show sample in verbose
    if args.verbose:
        for a in actions[:10]:
            print(f"[VERB]  - {a['name']} | {a['hash']} -> folder: {a['target_folder']}", file=sys.stderr)
        if len(actions) > 10:
            print(f"[VERB] ...and {len(actions)-10} more", file=sys.stderr)

    # If DRY_RUN -> write plan and exit
    if DRY_RUN:
        plan = []
        for a in actions:
            plan.append({
                "hash": a["hash"],
                "name": a["name"],
                "from_save_path": a["save_path"],
                "to_folder": a["target_folder"],
                "note": "DRY_RUN - no action performed"
            })
        OUT_PLAN.write_text(json.dumps({"dry_run": True, "plan": plan}, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.verbose:
            print(f"[VERB] Dry-run plan written to {OUT_PLAN}", file=sys.stderr)
        else:
            print(f"[INFO] DRY_RUN plan saved to {OUT_PLAN}", file=sys.stderr)
        return 0

    # If applying, confirm unless --yes
    if not args.yes:
        if not sys.stdin.isatty():
            print("[ERROR] Non-interactive terminal and --apply used without --yes => abort", file=sys.stderr)
            return 1
        ans = input(f"[CONFIRM] Apply setLocation+recheck on {len(actions)} torrents ? [y/N]: ").strip().lower()
        if ans not in ("y","yes","o","oui"):
            print("[INFO] Aborted by user.", file=sys.stderr)
            return 0

    # APPLY actions
    results = []
    for a in actions:
        h = a["hash"]
        tgt_folder = a["target_folder"]
        row: Dict[str, Any] = {"hash": h, "name": a["name"], "ok_setLocation": False, "ok_recheck": False, "state_after": None, "notes": []}

        if not tgt_folder:
            row["notes"].append("no target_folder in mapping")
            results.append(row)
            if args.verbose:
                print(f"[WARN] skipping {h}: no target_folder", file=sys.stderr)
            continue

        # setLocation
        try:
            if args.verbose:
                print(f"[ACTION] setLocation {h} -> {tgt_folder}", file=sys.stderr)
            qb_set_location(s, args.host, h, tgt_folder)
            row["ok_setLocation"] = True
            time.sleep(SLEEP_AFTER_SETLOCATION)
        except Exception as e:
            row["notes"].append(f"setLocation failed: {e}")
            results.append(row)
            if args.verbose:
                print(f"[ERR] setLocation failed for {h}: {e}", file=sys.stderr)
            continue

        # recheck
        try:
            if args.verbose:
                print(f"[ACTION] recheck {h}", file=sys.stderr)
            qb_recheck(s, args.host, h)
            row["ok_recheck"] = True
        except Exception as e:
            row["notes"].append(f"recheck failed: {e}")
            results.append(row)
            if args.verbose:
                print(f"[ERR] recheck failed for {h}: {e}", file=sys.stderr)
            continue

        # wait and fetch state after recheck
        time.sleep(SLEEP_AFTER_RECHECK)
        try:
            tinfo = qb_get_torrent_info(s, args.host, h)
            row["state_after"] = tinfo.get("state") if tinfo else None
        except Exception as e:
            row["notes"].append(f"fetch info after recheck failed: {e}")
            if args.verbose:
                print(f"[WARN] cannot fetch torrent info after recheck for {h}: {e}", file=sys.stderr)

        results.append(row)

    # Save results
    OUT_RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.verbose:
        print(f"[VERB] Apply results written to {OUT_RESULTS}", file=sys.stderr)
    else:
        print(f"[INFO] Apply results saved to {OUT_RESULTS}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    rc = main()
    if isinstance(rc, int) and rc != 0:
        sys.exit(rc)
