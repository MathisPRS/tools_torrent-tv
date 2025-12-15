#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radarr_history_export_clean.py

- Récupère tout l'historique via l'API Radarr (/api/v3/history) en paginant.
- Pour chaque film : récupère le dernier hash utilisé (downloadId / torrentInfoHash / torrentHash),
  le titre, tmdbId / movieId, le dernier événement, le chemin importé si disponible, le dossier parent.
- Écrit deux fichiers : un .txt lisible et un .json structuré.

Modifier la section CONFIG en haut pour BASE_URL / API_KEY etc.
"""

import requests
import time
import json
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------
# CONFIG (modifier ici)
# ---------------------------
BASE_URL = "https://radarr.infra-prs.fr"        # ex: "http://192.168.10.100:7878"
API_KEY = "PUT_YOUR_API_KEY_HERE"         # <-- mettre ta clé API Radarr
PAGE_SIZE = 200
INCLUDE_MOVIE = True
SLEEP_BETWEEN_PAGES = 0.05
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5

OUT_TXT = Path("/mnt/data/radarr_history_laststate.txt")
OUT_JSON = Path("/mnt/data/radarr_history_laststate.json")
# ---------------------------

# ---------------- helpers ----------------
def iso_parse_safe(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

def datetime_min():
    return datetime(1970,1,1, tzinfo=timezone.utc)

def normalize_path(p: str) -> str:
    if not p:
        return p
    s = p.replace("\\", "/")
    while '//' in s:
        s = s.replace('//', '/')
    return s.strip()

def request_with_retry(url, headers, params=None, timeout=30):
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise RuntimeError("Réponse non-JSON de l'API Radarr")
            if r.status_code == 429:
                wait = (BACKOFF_FACTOR ** attempt) * 2
                time.sleep(wait)
            elif 400 <= r.status_code < 500:
                r.raise_for_status()
            else:
                wait = (BACKOFF_FACTOR ** attempt) * 1.5
                time.sleep(wait)
        except requests.RequestException:
            wait = (BACKOFF_FACTOR ** attempt) * 1.0
            time.sleep(wait)
        attempt += 1
    raise RuntimeError("Échec après plusieurs tentatives de récupération de l'API Radarr")

# safe access to raw.data
def safe_raw(ev):
    raw = ev.get('raw') or {}
    if not isinstance(raw, dict):
        raw = {}
    data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
    return raw, data

def get_from_event(ev, *keys):
    raw, data = safe_raw(ev)
    for k in keys:
        v = data.get(k)
        if v is not None:
            return v
        v = raw.get(k)
        if v is not None:
            return v
        v = ev.get(k)
        if v is not None:
            return v
    return None

# fetch all history pages
def fetch_all_history(base_url, api_key, page_size=200, include_movie=True):
    headers = {'X-Api-Key': api_key}
    all_items = []
    page = 1
    while True:
        params = {'page': page, 'pageSize': page_size}
        if include_movie:
            params['includeMovie'] = 'true'
        url = base_url.rstrip('/') + '/api/v3/history'
        print(f"[INFO] Fetch page {page}...", flush=True)
        data = request_with_retry(url, headers, params=params)
        if isinstance(data, list):
            page_items = data
        elif isinstance(data, dict):
            page_items = None
            for key in ('records','items','history','data','results'):
                if key in data and isinstance(data[key], list):
                    page_items = data[key]; break
            if page_items is None:
                raise RuntimeError("Réponse inattendue de l'API Radarr (dict sans liste).")
        else:
            raise RuntimeError("Réponse inattendue de l'API Radarr (format).")

        print(f"[INFO] -> reçus {len(page_items)} événements", flush=True)
        if not page_items:
            break
        all_items.extend(page_items)
        if len(page_items) < page_size:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return all_items

# group events by movie (prefer id if present)
def group_events_by_movie(history_items):
    grouped = {}
    for ev in history_items:
        movie_info = ev.get('movie') or ev.get('Movie') or {}
        mid = None
        if isinstance(movie_info, dict):
            mid = movie_info.get('id') or movie_info.get('tmdbId') or movie_info.get('movieId')
        title = (movie_info.get('title') if isinstance(movie_info, dict) else None) or ev.get('title') or "Unknown Title"
        key = str(mid or title)
        grouped.setdefault(key, {'movie': movie_info if isinstance(movie_info, dict) else {}, 'events': []})
        grouped[key]['events'].append(ev)
    return grouped

# get event date
def event_date(ev):
    d = ev.get('date_iso') or ev.get('date') or ev.get('Date')
    dt = iso_parse_safe(d)
    return dt or datetime_min()

# find last hash used for a set of events (search latest event that has a hash-like field)
def find_last_hash(events):
    # sort events by date ascending
    sorted_ev = sorted(events, key=lambda e: event_date(e))
    # iterate from last to first, return first found hash-like field
    for ev in reversed(sorted_ev):
        # possible fields
        h = ev.get('downloadId') or get_from_event(ev, 'downloadId', 'torrentInfoHash', 'torrentHash', 'id')
        if h:
            return str(h)
    return None

# find importedFilePath or movieFile.path or movie.path
def find_best_filepath(movie_obj, events):
    # 1) check movie-level movieFile.path
    movie = movie_obj or {}
    mf = movie.get('movieFile') or movie.get('MovieFile') or {}
    if isinstance(mf, dict):
        p = mf.get('path') or mf.get('relativePath')
        if p:
            return normalize_path(p)
    # 2) check last events for importedFilePath or importedPath or event.movieFile
    # iterate events newest first
    for ev in sorted(events, key=lambda e: event_date(e), reverse=True):
        raw, data = safe_raw(ev)
        ipf = data.get('importedFilePath') or raw.get('importedFilePath') or ev.get('importedFilePath')
        if ipf:
            return normalize_path(ipf)
        ip = data.get('importedPath') or raw.get('importedPath') or ev.get('importedPath')
        if ip:
            # folder-only; keep as folder candidate
            return normalize_path(ip)
        # event-level movieFile
        mvf = data.get('movieFile') or raw.get('movieFile') or ev.get('movieFile')
        if isinstance(mvf, dict):
            p = mvf.get('path') or mvf.get('relativePath')
            if p:
                return normalize_path(p)
    # 3) fallback to movie.path / folderName
    mp = movie.get('path') or movie.get('folderName')
    if mp:
        return normalize_path(mp)
    return None

# extract last event type and date
def get_last_event_info(events):
    if not events:
        return None, None
    ev = max(events, key=lambda e: event_date(e))
    etype = ev.get('eventType') or ev.get('type') or ev.get('name') or ev.get('status') or None
    d = ev.get('date_iso') or ev.get('date') or ev.get('Date')
    return etype, (d or None)

# main
def main():
    if not API_KEY or API_KEY == "PUT_YOUR_API_KEY_HERE":
        print("[ERROR] Configure BASE_URL et API_KEY en haut du script.", flush=True)
        return

    print("[INFO] Récupération de l'historique Radarr...", flush=True)
    history = fetch_all_history(BASE_URL, API_KEY, page_size=PAGE_SIZE, include_movie=INCLUDE_MOVIE)
    print(f"[INFO] Total événements récupérés: {len(history)}", flush=True)

    grouped = group_events_by_movie(history)
    print(f"[INFO] Total films détectés: {len(grouped)}", flush=True)

    out_lines = []
    out_json = {}
    idx = 0

    for key, obj in grouped.items():
        idx += 1
        movie_info = obj.get('movie') or {}
        events = obj.get('events') or []

        title = movie_info.get('title') or movie_info.get('originalTitle') or movie_info.get('name') or "Unknown"
        tmdb = movie_info.get('tmdbId') or movie_info.get('id') or None
        movie_id = movie_info.get('id') or None
        events_count = len(events)

        last_hash = find_last_hash(events)  # may be None
        imported_fp = find_best_filepath(movie_info, events)
        folder = str(Path(imported_fp).parent) if imported_fp else None
        last_event_type, last_seen = get_last_event_info(events)

        record = {
            "title": title,
            "tmdbId": tmdb,
            "movieId": movie_id,
            "events_count": events_count,
            "lastHash": last_hash,
            "last_event_type": last_event_type,
            "lastSeen": last_seen,
            "importedFilePath": imported_fp,
            "folder": folder
        }

        out_json[key] = record

        out_lines.append(f"Film {idx}: {title} (tmdbId={tmdb})")
        if last_seen:
            out_lines.append(f"  lastSeen: {last_seen}")
        out_lines.append(f"  lastHash: {last_hash or 'None'}")
        out_lines.append(f"  importedFilePath: {imported_fp or 'None'}")
        out_lines.append(f"  folder: {folder or 'None'}")
        out_lines.append(f"  events_count: {events_count}")
        out_lines.append("")

    # write outputs
    OUT_TXT.write_text("\n".join(out_lines), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(out_json, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Écrit {len(out_json)} films dans {OUT_TXT} et {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
