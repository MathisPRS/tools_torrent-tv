#!/usr/bin/env python3
import os, sys, argparse, logging, json
from collections import defaultdict
from datetime import datetime, timezone

# permettre "import cleaner.*"
ROOT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

import requests

from cleaner.config import (
    SONARR_URL, SONARR_KEY, RADARR_URL, RADARR_KEY,
    QBIT_HOST, QBIT_USER, QBIT_PASS,
    HIST_PAGE_SIZE, HIST_MAX_PAGES
)
from cleaner.http import json_get

CATALOG_PATH = os.environ.get("CATALOG_FILE", os.path.join(ROOT, "data", "catalog.json"))
REQ_TIMEOUT = 20

log = logging.getLogger("catalog-builder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- utils ----------
def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_dt(iso_s: str) -> datetime:
    try:
        return datetime.fromisoformat((iso_s or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

def qb_login(sess: requests.Session):
    r = sess.post(f"{QBIT_HOST}/api/v2/auth/login",
                  data={"username": QBIT_USER, "password": QBIT_PASS},
                  timeout=REQ_TIMEOUT)
    if r.text.strip() != "Ok.":
        raise RuntimeError(f"qBittorrent login failed: {r.text}")

def qb_all_torrents(sess: requests.Session) -> list[dict]:
    r = sess.get(f"{QBIT_HOST}/api/v2/torrents/info", timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()

def sonarr_history_pages(max_pages: int):
    page = 1
    while page <= max_pages:
        payload = json_get(f"{SONARR_URL}/api/v3/history",
                           headers={"X-Api-Key": SONARR_KEY},
                           params={
                               "includeEpisode": "true",
                               "page": page, "pageSize": HIST_PAGE_SIZE,
                               "sortKey": "date", "sortDirection": "descending"
                           })
        recs = payload.get("records", payload) or []
        if not recs: break
        yield recs
        total = payload.get("totalRecords")
        if total is not None and page * HIST_PAGE_SIZE >= total: break
        page += 1

def radarr_history_pages(max_pages: int):
    page = 1
    while page <= max_pages:
        payload = json_get(f"{RADARR_URL}/api/v3/history",
                           headers={"X-Api-Key": RADARR_KEY},
                           params={
                               "includeMovie": "true",
                               "page": page, "pageSize": HIST_PAGE_SIZE,
                               "sortKey": "date", "sortDirection": "descending"
                           })
        recs = payload.get("records", payload) or []
        if not recs: break
        yield recs
        total = payload.get("totalRecords")
        if total is not None and page * HIST_PAGE_SIZE >= total: break
        page += 1

def is_rel_sonarr(ev: str) -> bool:
    ev = (ev or "").lower()
    return ev in ("grabbed","grab","download","downloadimported","episodefileimported","upgrade","downloadfolderimported")

def is_rel_radarr(ev: str) -> bool:
    ev = (ev or "").lower()
    return ev in ("grabbed","grab","download","moviefileimported","downloadfolderimported","upgrade")

def ensure_dir(p: str):
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def load_catalog(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sonarr": {}, "radarr": {}, "meta": {}}

def save_catalog(cat: dict, path: str):
    ensure_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ---------- merge helpers ----------
def merge_episode(entry: dict, add_hash: str, event_dt: datetime, title: str | None, season: int | None, epnum: int | None):
    entry.setdefault("season", season)
    entry.setdefault("episode", epnum)
    if title: entry.setdefault("title", title)
    entry.setdefault("candidates", [])
    entry.setdefault("removed", [])
    entry.setdefault("latest", None)
    cur = (add_hash or "").lower().strip()
    if cur and cur not in entry["candidates"]:
        entry["candidates"].append(cur)
    # latest = plus récent (on ne garde pas la date individuelle, seulement la max globale)
    max_prev = parse_dt(entry.get("max_event_at") or "1970-01-01T00:00:00Z")
    if event_dt > max_prev:
        entry["max_event_at"] = iso(event_dt)
        entry["latest"] = cur or entry["latest"]

def merge_movie(entry: dict, add_hash: str, event_dt: datetime, title: str | None, year: int | None):
    if title: entry.setdefault("title", title)
    if year:  entry.setdefault("year", year)
    entry.setdefault("candidates", [])
    entry.setdefault("removed", [])
    entry.setdefault("latest", None)
    cur = (add_hash or "").lower().strip()
    if cur and cur not in entry["candidates"]:
        entry["candidates"].append(cur)
    max_prev = parse_dt(entry.get("max_event_at") or "1970-01-01T00:00:00Z")
    if event_dt > max_prev:
        entry["max_event_at"] = iso(event_dt)
        entry["latest"] = cur or entry["latest"]

# ---------- build ----------
def build_catalog(pages_sonarr: int, pages_radarr: int, qb_only: bool):
    # 1) qB (optionnel pour filtrer)
    with requests.Session() as sess:
        log.info("Connexion qBittorrent…")
        qb_login(sess)
        qbt = qb_all_torrents(sess)
    qb_hashes = { (t.get("hash") or "").lower(): t for t in qbt }
    log.info(f"qB: {len(qb_hashes)} torrents chargés.")

    # 2) charger existant (merge, pas overwrite)
    catalog = load_catalog(CATALOG_PATH)

    # 3) SONARR
    if SONARR_URL and SONARR_KEY:
        total_events = 0
        log.info(f"Scan Sonarr: {pages_sonarr} pages…")
        for recs in sonarr_history_pages(max(1, pages_sonarr)):
            for it in recs:
                if not is_rel_sonarr(it.get("eventType")): continue
                dl = (it.get("downloadId") or "").lower().strip()
                if qb_only and (not dl or dl not in qb_hashes): continue

                series_id = it.get("seriesId"); series = it.get("series") or {}
                ep = it.get("episode") or {}
                eid = ep.get("id") or it.get("episodeId")
                if not (series_id and eid): continue

                event_dt = parse_dt(it.get("date"))
                series_entry = catalog["sonarr"].setdefault(str(series_id), {"seriesTitle": series.get("title"), "episodes": {}})
                epi_entry = series_entry["episodes"].setdefault(str(eid), {})
                merge_episode(
                    epi_entry, dl, event_dt,
                    title=ep.get("title"),
                    season=ep.get("seasonNumber"),
                    epnum=ep.get("episodeNumber"),
                )
                total_events += 1
        log.info(f"Sonarr: fusion de {total_events} événements pertinents.")
    else:
        log.info("Sonarr désactivé (URL/API KEY manquants).")

    # 4) RADARR
    if RADARR_URL and RADARR_KEY:
        total_events = 0
        log.info(f"Scan Radarr: {pages_radarr} pages…")
        for recs in radarr_history_pages(max(1, pages_radarr)):
            for it in recs:
                if not is_rel_radarr(it.get("eventType")): continue
                dl = (it.get("downloadId") or "").lower().strip()
                if qb_only and (not dl or dl not in qb_hashes): continue

                movie = it.get("movie") or {}
                mid = movie.get("id") or it.get("movieId")
                if not mid: continue

                event_dt = parse_dt(it.get("date"))
                mov_entry = catalog["radarr"].setdefault(str(mid), {})
                merge_movie(
                    mov_entry, dl, event_dt,
                    title=movie.get("title"), year=movie.get("year")
                )
                total_events += 1
        log.info(f"Radarr: fusion de {total_events} événements pertinents.")
    else:
        log.info("Radarr désactivé (URL/API KEY manquants).")

    # 5) meta + save
    catalog.setdefault("meta", {})
    catalog["meta"]["built_at"] = iso(datetime.now(timezone.utc))
    catalog["meta"]["source"] = "build_catalog"
    catalog["meta"]["sonarr"] = {"pages_scanned": pages_sonarr}
    catalog["meta"]["radarr"] = {"pages_scanned": pages_radarr}

    ensure_dir(CATALOG_PATH)
    save_catalog(catalog, CATALOG_PATH)
    log.info(f"Catalogue écrit → {CATALOG_PATH}")
    log.info("Terminé ✅")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Construit un catalogue hiérarchique Sonarr/Radarr à partir de qB + historiques.")
    ap.add_argument("--pages-sonarr", type=int, default=max(10, HIST_MAX_PAGES),
                    help="Pages d'historique Sonarr à scanner (tri DESC).")
    ap.add_argument("--pages-radarr", type=int, default=max(10, HIST_MAX_PAGES),
                    help="Pages d'historique Radarr à scanner (tri DESC).")
    ap.add_argument("--qb-only", action="store_true",
                    help="Ne catalogue que les hashes présents actuellement dans qBittorrent.")
    args = ap.parse_args()
    build_catalog(args.pages_sonarr, args.pages_radarr, args.qb_only)

if __name__ == "__main__":
    main()
