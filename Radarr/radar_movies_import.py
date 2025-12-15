#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radarr_to_qb_export.py

Export Radarr movies + local file(s) (.mkv) and try to match to qBittorrent torrent hashes.
Output: JSON list with entries:
{
  "radarr_id": <int>,
  "title": "<Movie Title>",
  "moviefile_path": "<full path from Radarr movieFile.path>",
  "folder": "<parent folder>",
  "mkv_files": ["...mkv", ...],
  "matched_torrents": [
     {"hash": "<torrent hash>", "name": "<torrent name>", "save_path": "<save_path>", "matched_by": "save_path|filename|files_list"}
  ]
}

Usage:
  export RADARR_HOST="http://127.0.0.1:7878"
  export RADARR_APIKEY="XXXX"
  export QBT_HOST="http://127.0.0.1:8080"
  export QBT_USER="admin"
  export QBT_PASS="adminadmin"
  python3 radarr_to_qb_export.py --out output.json

Or pass hosts and keys with CLI options.

"""
from __future__ import annotations
import os
import sys
import argparse
import requests
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

TIMEOUT = 15.0
DEFAULT_OUT = Path("radarr_movies_with_hashes.json")

# ---------------- Radarr ----------------
def radarr_get_movies(host: str, apikey: str) -> List[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/v3/movie"
    r = requests.get(url, params={"apikey": apikey}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

# ---------------- qBittorrent ----------------
def qb_login(s: requests.Session, host: str, user: str, password: str):
    url = host.rstrip("/") + "/api/v2/auth/login"
    r = s.post(url, data={"username": user, "password": password}, timeout=TIMEOUT)
    r.raise_for_status()
    if "SID" not in s.cookies.get_dict():
        raise RuntimeError("qBittorrent login failed: no SID cookie")

def qb_get_torrents(s: requests.Session, host: str) -> List[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/v2/torrents/info"
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def qb_get_torrent_files(s: requests.Session, host: str, hash_: str) -> List[Dict[str, Any]]:
    url = host.rstrip("/") + "/api/v2/torrents/files"
    r = s.get(url, params={"hash": hash_}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

# ---------------- helpers ----------------
def normalize_path(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return str(Path(p))

def parent_folder_of_path(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return str(Path(p).parent)

def find_mkv_in_moviefile(moviefile_path: Optional[str]) -> List[str]:
    """
    Radarr usually fills movieFile.path with the full path to the movie file.
    We treat that as the primary mkv (if it endswith .mkv). If movieFile.path is a folder,
    this script cannot list the filesystem — we only parse what Radarr provides.
    """
    if not moviefile_path:
        return []
    p = Path(moviefile_path)
    if p.suffix.lower() == ".mkv":
        return [p.name]
    # if Radarr gives a folder or unknown extension, still return the basename if it looks like mkv
    if str(p).lower().endswith(".mkv"):
        return [p.name]
    return []

# ---------------- main ----------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Export Radarr movies and try to match local .mkv to qBittorrent hashes.")
    parser.add_argument("--radarr-host", default=os.environ.get("RADARR_HOST", "http://127.0.0.1:7878"), help="Radarr host (include http:// and port)")
    parser.add_argument("--radarr-apikey", default=os.environ.get("RADARR_APIKEY"), help="Radarr API key (or set RADARR_APIKEY env)")
    parser.add_argument("--qbt-host", default=os.environ.get("QBT_HOST", "http://127.0.0.1:8080"), help="qBittorrent host")
    parser.add_argument("--qbt-user", default=os.environ.get("QBT_USER", "admin"), help="qBittorrent user")
    parser.add_argument("--qbt-pass", default=os.environ.get("QBT_PASS", "adminadmin"), help="qBittorrent password")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON file")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args(argv)

    if not args.radarr_apikey:
        print("[ERROR] Radarr API key not provided. Set --radarr-apikey or RADARR_APIKEY env.", file=sys.stderr)
        return 2

    # fetch radarr movies
    try:
        movies = radarr_get_movies(args.radarr_host, args.radarr_apikey)
    except Exception as e:
        print(f"[ERROR] Failed to fetch movies from Radarr: {e}", file=sys.stderr)
        return 3

    if args.verbose:
        print(f"[VERB] Fetched {len(movies)} movies from Radarr", file=sys.stderr)

    # login to qBittorrent and fetch torrents
    s = requests.Session()
    try:
        qb_login(s, args.qbt_host, args.qbt_user, args.qbt_pass)
    except Exception as e:
        print(f"[ERROR] qBittorrent login failed: {e}", file=sys.stderr)
        return 4

    try:
        torrents = qb_get_torrents(s, args.qbt_host)
    except Exception as e:
        print(f"[ERROR] Failed to fetch torrents from qBittorrent: {e}", file=sys.stderr)
        return 5

    if args.verbose:
        print(f"[VERB] Fetched {len(torrents)} torrents from qBittorrent", file=sys.stderr)

    # build simple index by save_path for quick matching
    torrents_by_savepath: Dict[str, List[Dict[str,Any]]] = {}
    for t in torrents:
        sp = normalize_path(t.get("save_path") or t.get("savePath") or "")
        torrents_by_savepath.setdefault(sp, []).append(t)

    output = []
    # iterate movies
    for m in movies:
        radarr_id = m.get("id")
        title = m.get("title") or m.get("titleSlug") or ""
        moviefile = m.get("movieFile")  # may be None if not imported
        moviefile_path = None
        mkv_files = []
        folder = None

        if moviefile:
            # Radarr's movieFile.path is usually the full path to the file (or sometimes folder)
            moviefile_path = moviefile.get("path") or moviefile.get("relativePath")
            moviefile_path = normalize_path(moviefile_path)
            if moviefile_path:
                folder = parent_folder_of_path(moviefile_path)
                mkv_files = find_mkv_in_moviefile(moviefile_path)

        entry = {
            "radarr_id": radarr_id,
            "title": title,
            "moviefile_path": moviefile_path,
            "folder": folder,
            "mkv_files": mkv_files,
            "matched_torrents": []
        }

        # Try to match torrents:
        matched = []

        # 1) quick match by save_path == folder
        if folder:
            candidates = torrents_by_savepath.get(folder, [])
            for t in candidates:
                matched.append({"torrent": t, "reason": "save_path"})

        # 2) if mkv filename exists, try to match by filename inside torrent files (slower)
        if mkv_files:
            # scan all torrents and look for filename in torrent name or file list
            # prefer checking name first (cheap)
            fname = mkv_files[0].lower()
            for t in torrents:
                tname = (t.get("name") or "").lower()
                if fname in tname:
                    matched.append({"torrent": t, "reason": "name_contains_filename"})
                else:
                    # fallback: ask qBittorrent for torrent file list if not matched yet
                    try:
                        files = qb_get_torrent_files(s, args.qbt_host, t.get("hash"))
                        for f in files:
                            # 'name' usually contains relative path inside torrent
                            if fname == (f.get("name") or "").lower().split("/")[-1]:
                                matched.append({"torrent": t, "reason": "files_list"})
                                break
                    except Exception:
                        # ignore per-torrent file list errors (timeouts)
                        continue

        # 3) deduplicate matched by hash
        seen_hashes = set()
        for mobj in matched:
            t = mobj["torrent"]
            h = (t.get("hash") or "").upper()
            if not h or h in seen_hashes:
                continue
            seen_hashes.add(h)
            entry["matched_torrents"].append({
                "hash": h,
                "name": t.get("name"),
                "save_path": normalize_path(t.get("save_path") or t.get("savePath") or ""),
                "matched_by": mobj["reason"]
            })

        output.append(entry)

    # write output JSON
    try:
        args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[INFO] Wrote {len(output)} entries to {args.out}", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] Failed to write output JSON: {e}", file=sys.stderr)
        return 6

    return 0

if __name__ == "__main__":
    rc = main()
    if isinstance(rc, int) and rc != 0:
        sys.exit(rc)
