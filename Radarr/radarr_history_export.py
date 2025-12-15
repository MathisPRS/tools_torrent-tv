#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radarr_history_export_clean2.py

Récupère l'historique Radarr (pagination) et pour chaque film:
 - récupère le dernier hash utilisé
 - retourne title, tmdbId, movieId, lastSeen, last_event_type
 - calcule importedFilePath (chemin complet du .mkv si possible)
 - calcule folder (dossier complet contenant le MKV)
Écrit deux fichiers: OUT_TXT et OUT_JSON.
"""

import requests
import time
import json
from pathlib import Path
from datetime import datetime, timezone
import re

# ---------------------------
# CONFIG (modifier ici)
# ---------------------------
BASE_URL = "http://localhost:7878"        # ex: "http://192.168.10.100:7878"
API_KEY = "PUT_YOUR_API_KEY_HERE"         # <-- mettre ta clé API Radarr
PAGE_SIZE = 200
INCLUDE_MOVIE = True
SLEEP_BETWEEN_PAGES = 0.05
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5

OUT_TXT = Path("./radarr_history_laststate.txt")
OUT_JSON = Path("./radarr_history_laststate.json")
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
    # remove repeated slashes but keep leading slash
    s = re.sub(r'/{2,}', '/', s)
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

# safe raw access
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

def event_date(ev):
    d = ev.get('date_iso') or ev.get('date') or ev.get('Date')
    dt = iso_parse_safe(d)
    return dt or datetime_min()

# find last hash used for a set of events
def find_last_hash(events):
    sorted_ev = sorted(events, key=lambda e: event_date(e))
    for ev in reversed(sorted_ev):
        # check common fields that may contain the torrent/download hash/id
        h = ev.get('downloadId') or get_from_event(ev, 'downloadId', 'torrentInfoHash', 'torrentHash', 'id')
        if h:
            return str(h)
    return None

# construct a plausible filename from title/year/tmdb
def build_filename(title, year, tmdb):
    safe_title = (title or "Unknown").replace("/", "-")
    if year:
        return f"{safe_title} ({year}) {tmdb or ''}.mkv".strip()
    return f"{safe_title} {tmdb or ''}.mkv".strip()

# find the best importedFilePath (complete path to MKV) and folder (parent folder)
def find_best_filepath_and_folder(movie_obj, events):
    movie = movie_obj or {}
    # 1) try movie-level movieFile.path (full path to file)
    mf = movie.get('movieFile') or movie.get('MovieFile') or {}
    movie_path_field = movie.get('path') or movie.get('folderName')
    title = movie.get('title') or movie.get('originalTitle') or movie.get('name') or None
    tmdb = movie.get('tmdbId') or movie.get('id') or None
    year = movie.get('year')

    if isinstance(mf, dict):
        p = mf.get('path')
        rel = mf.get('relativePath') or None
        if p:
            fp = normalize_path(p)
            folder = str(Path(fp).parent)
            return fp, folder
        if rel and movie_path_field:
            # relativePath often like "Title (Year)/Title (Year).mkv"
            # construct full path
            rel_norm = normalize_path(rel)
            fp = str(Path(normalize_path(movie_path_field)) / Path(rel_norm).name)
            fp = normalize_path(fp)
            folder = str(Path(fp).parent)
            return fp, folder

    # 2) scan events newest first for importedFilePath or importedPath or event.movieFile
    for ev in sorted(events, key=lambda e: event_date(e), reverse=True):
        raw, data = safe_raw(ev)
        # explicit importedFilePath (file)
        ipf = data.get('importedFilePath') or raw.get('importedFilePath') or ev.get('importedFilePath')
        if ipf:
            fp = normalize_path(ipf)
            folder = str(Path(fp).parent)
            return fp, folder
        # importedPath (folder only)
        ip = data.get('importedPath') or raw.get('importedPath') or ev.get('importedPath')
        if ip:
            folder_candidate = normalize_path(ip)
            # Try to infer filename: prefer movie.movieFile.rel if any in events later
            # search for movieFile in this event
            mvf = data.get('movieFile') or raw.get('movieFile') or ev.get('movieFile')
            if isinstance(mvf, dict):
                p = mvf.get('path') or mvf.get('relativePath')
                if p:
                    # if p is relative path, use basename
                    name = Path(normalize_path(p)).name
                    fp = str(Path(folder_candidate) / name)
                    return normalize_path(fp), str(Path(fp).parent)
            # fallback: build plausible filename from title/year/tmdb
            if title or tmdb:
                name = build_filename(title, year, tmdb)
                fp = str(Path(folder_candidate) / name)
                return normalize_path(fp), str(Path(fp).parent)
            # else return folder only (no filename)
            return folder_candidate, str(Path(folder_candidate).parent)

        # event-level movieFile
        mvf = data.get('movieFile') or raw.get('movieFile') or ev.get('movieFile')
        if isinstance(mvf, dict):
            p = mvf.get('path') or mvf.get('relativePath')
            if p:
                p_norm = normalize_path(p)
                # if relative and movie_path_field exists, construct
                if movie_path_field and not Path(p_norm).is_absolute():
                    fp = str(Path(normalize_path(movie_path_field)) / Path(p_norm).name)
                else:
                    fp = p_norm
                fp = normalize_path(fp)
                folder = str(Path(fp).parent)
                return fp, folder

    # 3) fallback to movie.path (folder) and construct plausible filename if possible
    if movie_path_field:
        folder = normalize_path(movie_path_field)
        if title or tmdb:
            name = build_filename(title, year, tmdb)
            fp = str(Path(folder) / name)
            return normalize_path(fp), str(Path(fp).parent)
        return folder, str(Path(folder).parent)

    return None, None

def get_last_event_info(events):
    if not events:
        return None, None
    ev = max(events, key=lambda e: event_date(e))
    etype = ev.get('eventType') or ev.get('type') or ev.get('name') or ev.get('status') or None
    d = ev.get('date_iso') or ev.get('date') or ev.get('Date')
    return etype, (d or None)

# ---------------- main ----------------
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
        imported_fp, folder = find_best_filepath_and_folder(movie_info, events)
        # ensure folder ends with single slash removed? keep no trailing slash but folder is full path
        if folder:
            folder = normalize_path(folder)
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
