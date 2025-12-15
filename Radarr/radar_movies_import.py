#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
radarr_export_all_movies.py

Export tous les films Radarr avec :
- nom du film
- id Radarr
- dossier
- nom du fichier .mkv
- hash du torrent qBittorrent correspondant (si trouvé)

Résultat : JSON
"""

from __future__ import annotations
import requests
import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

# ============================================================
# ========================= CONFIG ===========================
# ============================================================

RADARR_HOST = "http://127.0.0.1:7878"
RADARR_APIKEY = "PUT_RADARR_API_KEY_HERE"

QBT_HOST = "http://127.0.0.1:8080"
QBT_USER = "admin"
QBT_PASS = "adminadmin"

OUTPUT_JSON = Path("radarr_movies_export.json")

REQUEST_TIMEOUT = 15.0
VERBOSE = True

# ============================================================
# ======================= RADARR API =========================
# ============================================================

def radarr_get_movies() -> List[Dict[str, Any]]:
    url = RADARR_HOST.rstrip("/") + "/api/v3/movie"
    r = requests.get(url, params={"apikey": RADARR_APIKEY}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ============================================================
# ===================== QBITTORRENT API ======================
# ============================================================

def qb_login(session: requests.Session):
    url = QBT_HOST.rstrip("/") + "/api/v2/auth/login"
    r = session.post(url, data={"username": QBT_USER, "password": QBT_PASS}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    if "SID" not in session.cookies.get_dict():
        raise RuntimeError("qBittorrent login failed")

def qb_get_torrents(session: requests.Session) -> List[Dict[str, Any]]:
    url = QBT_HOST.rstrip("/") + "/api/v2/torrents/info"
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def qb_get_torrent_files(session: requests.Session, hash_: str) -> List[Dict[str, Any]]:
    url = QBT_HOST.rstrip("/") + "/api/v2/torrents/files"
    r = session.get(url, params={"hash": hash_}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ============================================================
# ========================= HELPERS ==========================
# ============================================================

def normalize(p: Optional[str]) -> Optional[str]:
    return str(Path(p)) if p else None

def get_parent(p: Optional[str]) -> Optional[str]:
    return str(Path(p).parent) if p else None

def extract_mkv(moviefile_path: Optional[str]) -> Optional[str]:
    if not moviefile_path:
        return None
    p = Path(moviefile_path)
    if p.suffix.lower() == ".mkv":
        return p.name
    return None

# ============================================================
# ============================ MAIN ==========================
# ============================================================

def main():
    if not RADARR_APIKEY or "PUT_RADARR_API_KEY" in RADARR_APIKEY:
        print("[ERROR] Radarr API key non configurée", file=sys.stderr)
        return 1

    # --- Radarr ---
    movies = radarr_get_movies()
    if VERBOSE:
        print(f"[INFO] Radarr : {len(movies)} films trouvés", file=sys.stderr)

    # --- qBittorrent ---
    session = requests.Session()
    qb_login(session)
    torrents = qb_get_torrents(session)
    if VERBOSE:
        print(f"[INFO] qBittorrent : {len(torrents)} torrents chargés", file=sys.stderr)

    # Index torrents par save_path
    torrents_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for t in torrents:
        sp = normalize(t.get("save_path") or t.get("savePath"))
        torrents_by_path.setdefault(sp, []).append(t)

    output = []

    for m in movies:
        movie_id = m.get("id")
        title = m.get("title")
        moviefile = m.get("movieFile")

        moviefile_path = None
        folder = None
        mkv_name = None

        if moviefile:
            moviefile_path = normalize(moviefile.get("path"))
            folder = get_parent(moviefile_path)
            mkv_name = extract_mkv(moviefile_path)

        entry = {
            "radarr_id": movie_id,
            "title": title,
            "folder": folder,
            "mkv_file": mkv_name,
            "torrent_hash": None
        }

        # 1️⃣ Match direct par save_path
        if folder and folder in torrents_by_path:
            entry["torrent_hash"] = torrents_by_path[folder][0].get("hash")

        # 2️⃣ Fallback : recherche du fichier dans les torrents
        if not entry["torrent_hash"] and mkv_name:
            mkv_lower = mkv_name.lower()
            for t in torrents:
                try:
                    files = qb_get_torrent_files(session, t.get("hash"))
                    for f in files:
                        if mkv_lower == f.get("name", "").lower().split("/")[-1]:
                            entry["torrent_hash"] = t.get("hash")
                            break
                except Exception:
                    continue
                if entry["torrent_hash"]:
                    break

        output.append(entry)

    OUTPUT_JSON.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"[OK] Export terminé → {OUTPUT_JSON} ({len(output)} films)", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
