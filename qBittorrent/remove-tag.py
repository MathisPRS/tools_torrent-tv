#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qb_clear_restore_tag.py
Find all torrents with tag 'restore' and remove that tag.

Usage (dry-run):
  python3 qb_clear_restore_tag.py --host http://127.0.0.1:8080 --user admin --passw adminadmin

Apply:
  python3 qb_clear_restore_tag.py --apply --yes
"""
from __future__ import annotations
import argparse
import sys
import json
import requests
import os
from typing import List, Dict, Any, Optional

# Defaults (env override possible)
DEFAULT_QBT_HOST = os.environ.get("QBT_HOST", "http://127.0.0.1:8080")
DEFAULT_QBT_USER = os.environ.get("QBT_USER", "admin")
DEFAULT_QBT_PASS = os.environ.get("QBT_PASS", "adminadmin")

TIMEOUT = 15.0

# ---------------- qBittorrent API helpers ----------------
def qb_login(s: requests.Session, host: str, user: str, password: str):
    url = host.rstrip("/") + "/api/v2/auth/login"
    r = s.post(url, data={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    # success if cookie present
    if "SID" not in s.cookies.get_dict():
        raise RuntimeError(f"login failed: no session cookie returned (HTTP {r.status_code})")

def qb_get_torrents(s: requests.Session, host: str) -> List[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/v2/torrents/info"
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def qb_remove_tags(s: requests.Session, host: str, hashes: List[str], tags: List[str]) -> None:
    """
    Call /api/v2/torrents/removeTags with hashes and tags.
    hashes: list of torrent hashes (will be joined with |)
    tags: list of tag names to remove (joined with ,)
    """
    url = host.rstrip("/") + "/api/v2/torrents/removeTags"
    data = {
        "hashes": "|".join(hashes),
        "tags": ",".join(tags)
    }
    r = s.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return

# ---------------- helpers ----------------
def parse_tags(tag_str: Optional[str]) -> List[str]:
    if not tag_str:
        return []
    return [t.strip().lower() for t in tag_str.split(",") if t.strip()]

# ---------------- main ----------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove tag 'restore' from all torrents that have it (dry-run by default).")
    parser.add_argument("--host", default=DEFAULT_QBT_HOST, help="qBittorrent host (http://...:8080)")
    parser.add_argument("--user", default=DEFAULT_QBT_USER, help="qBittorrent user")
    parser.add_argument("--passw", default=DEFAULT_QBT_PASS, help="qBittorrent password")
    parser.add_argument("--tag", default="restore", help="Tag to remove (default: restore)")
    parser.add_argument("--apply", action="store_true", help="If set, actually remove the tag(s)")
    parser.add_argument("--yes", action="store_true", help="Bypass interactive confirmation when applying")
    parser.add_argument("--verbose", action="store_true", help="Verbose logs")
    args = parser.parse_args(argv)

    DRY_RUN = not args.apply
    if args.verbose:
        print(f"[VERB] DRY_RUN={DRY_RUN}", file=sys.stderr)

    s = requests.Session()
    try:
        qb_login(s, args.host, args.user, args.passw)
    except Exception as e:
        print(f"[ERROR] Login failed: {e}", file=sys.stderr)
        return 2

    try:
        all_torrents = qb_get_torrents(s, args.host)
    except Exception as e:
        print(f"[ERROR] Cannot fetch torrents: {e}", file=sys.stderr)
        return 3

    target_tag = args.tag.strip().lower()
    matches = []
    for t in all_torrents:
        tags = parse_tags(t.get("tags"))
        if target_tag in tags:
            matches.append({"hash": (t.get("hash") or ""), "name": t.get("name"), "tags": tags})

    # Print number found (as in your other script you printed a single int)
    print(len(matches))

    if not matches:
        if args.verbose:
            print("[VERB] No torrents found with that tag.", file=sys.stderr)
        return 0

    if DRY_RUN:
        # show sample and write plan if wanted
        if args.verbose:
            for m in matches[:10]:
                print(f"[VERB] Would remove tag '{target_tag}' from: {m['name']} ({m['hash']})", file=sys.stderr)
            if len(matches) > 10:
                print(f"[VERB] ...and {len(matches)-10} more", file=sys.stderr)
        else:
            print(f"[INFO] DRY_RUN: {len(matches)} torrents would have tag '{target_tag}' removed.", file=sys.stderr)
        return 0

    # APPLY: confirm unless --yes
    if not args.yes:
        if not sys.stdin.isatty():
            print("[ERROR] Non-interactive shell and --apply used without --yes => abort", file=sys.stderr)
            return 1
        ans = input(f"[CONFIRM] Remove tag '{target_tag}' from {len(matches)} torrents ? [y/N]: ").strip().lower()
        if ans not in ("y","yes","o","oui"):
            print("[INFO] Aborted by user.", file=sys.stderr)
            return 0

    # perform removal in batches (one call is fine for many hashes but keep it modest)
    BATCH_SIZE = 100
    hashes = [m["hash"] for m in matches if m["hash"]]
    failures = []
    for i in range(0, len(hashes), BATCH_SIZE):
        batch = hashes[i:i+BATCH_SIZE]
        try:
            if args.verbose:
                print(f"[ACTION] Removing tag '{target_tag}' from {len(batch)} torrents...", file=sys.stderr)
            qb_remove_tags(s, args.host, batch, [target_tag])
        except Exception as e:
            failures.append({"batch_start": i, "error": str(e)})
            if args.verbose:
                print(f"[ERR] removeTags failed for batch starting at {i}: {e}", file=sys.stderr)

    # report
    if failures:
        print(f"[WARN] Completed with {len(failures)} failed batch(es). See stderr logs.", file=sys.stderr)
        if args.verbose:
            print(json.dumps(failures, indent=2, ensure_ascii=False), file=sys.stderr)
        return 4

    print(f"[INFO] Successfully removed tag '{target_tag}' from {len(hashes)} torrents.", file=sys.stderr)
    return 0

if __name__ == "__main__":
    rc = main()
    if isinstance(rc, int) and rc != 0:
        sys.exit(rc)
