#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
qb_restore_from_input.py

- Sélectionne des torrents qBittorrent par tag (-t restore)
- Associe chaque hash avec un input JSON (Radarr-like) via lastHash
- Applique setLocation + recheck
- PAS de resume automatique
"""

import argparse
import json
import sys
import time
from pathlib import Path
import requests

# ---------------- CONFIG ----------------
QBT_HOST = "http://192.168.10.100:8080"
QBT_USER = "mreclus"
QBT_PASS = "********"
SLEEP_AFTER_SETLOCATION = 0.2
SLEEP_AFTER_RECHECK = 0.2
# ----------------------------------------

# ---------- qBittorrent API ----------
def qb_login(s, host, user, password):
    r = s.post(f"{host}/api/v2/auth/login",
               data={"username": user, "password": password},
               timeout=10)
    if r.status_code != 200 or "SID" not in s.cookies.get_dict():
        raise RuntimeError("qBittorrent login failed")

def qb_torrents(s, host):
    r = s.get(f"{host}/api/v2/torrents/info", timeout=30)
    r.raise_for_status()
    return r.json()

def qb_set_location(s, host, h, path):
    r = s.post(f"{host}/api/v2/torrents/setLocation",
               data={"hashes": h, "location": path},
               timeout=30)
    r.raise_for_status()

def qb_recheck(s, host, h):
    r = s.post(f"{host}/api/v2/torrents/recheck",
               data={"hashes": h},
               timeout=30)
    r.raise_for_status()

# ---------- utils ----------
def load_input_json(p: Path):
    data = json.loads(p.read_text(encoding="utf-8"))
    by_hash = {}
    for _, v in data.items():
        h = (v.get("lastHash") or "").upper()
        if h:
            by_hash[h] = v
    return by_hash

def parse_tags(tag_str):
    return [t.strip().lower() for t in (tag_str or "").split(",") if t.strip()]

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Restore torrents location from input JSON")
    ap.add_argument("-i", "--input", required=True, type=Path, help="Input JSON (Radarr export)")
    ap.add_argument("-t", "--tag", required=True, help="Tag qBittorrent à filtrer (ex: restore)")
    ap.add_argument("-c", "--category", help="Catégorie qBittorrent (optionnel)")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Dry-run (default)")
    ap.add_argument("--yes", action="store_true", help="Appliquer sans confirmation")
    args = ap.parse_args()

    # Load input JSON
    print(f"[INFO] Loading input JSON: {args.input}")
    input_map = load_input_json(args.input)
    print(f"[INFO] Hashes in input: {len(input_map)}")

    # Login qB
    s = requests.Session()
    qb_login(s, QBT_HOST, QBT_USER, QBT_PASS)

    torrents = qb_torrents(s, QBT_HOST)

    # Filter torrents
    selected = []
    for t in torrents:
        tags = parse_tags(t.get("tags"))
        if args.tag.lower() not in tags:
            continue
        if args.category and (t.get("category") or "").lower() != args.category.lower():
            continue
        selected.append(t)

    print(f"[INFO] Torrents with tag '{args.tag}': {len(selected)}")

    # Build actions
    actions = []
    for t in selected:
        h = t.get("hash", "").upper()
        src = input_map.get(h)
        if not src:
            continue
        actions.append({
            "hash": h,
            "name": t.get("name"),
            "state": t.get("state"),
            "from": t.get("save_path"),
            "to": src.get("folder"),
            "title": src.get("title")
        })

    print(f"[INFO] Torrents with matching input hash: {len(actions)}")

    if not actions:
        print("[INFO] Nothing to do.")
        return 0

    # Summary
    print("\n=== RESTORE PLAN ===")
    for a in actions[:10]:
        print(f"- {a['title']} | {a['hash']} → {a['to']}")
    if len(actions) > 10:
        print(f"... and {len(actions)-10} more")
    print("====================\n")

    if args.dry_run:
        print("[DRY-RUN] No changes applied.")
        return 0

    if not args.yes:
        ans = input(f"Apply setLocation + recheck to {len(actions)} torrents ? [y/N]: ").lower()
        if ans not in ("y", "yes", "o", "oui"):
            print("[INFO] Cancelled.")
            return 0

    # APPLY
    results = []
    for a in actions:
        try:
            print(f"[ACTION] setLocation {a['hash']} → {a['to']}")
            qb_set_location(s, QBT_HOST, a["hash"], a["to"])
            time.sleep(SLEEP_AFTER_SETLOCATION)

            print(f"[ACTION] recheck {a['hash']}")
            qb_recheck(s, QBT_HOST, a["hash"])
            time.sleep(SLEEP_AFTER_RECHECK)

            results.append({"hash": a["hash"], "ok": True})
        except Exception as e:
            results.append({"hash": a["hash"], "ok": False, "error": str(e)})

    Path("qb_restore_results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"[INFO] Done. Results saved to qb_restore_results.json")
    return 0

if __name__ == "__main__":
    sys.exit(main())
