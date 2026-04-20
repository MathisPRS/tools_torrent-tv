#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from collections import defaultdict
from guessit import guessit
import requests

# ==============================
# ===== CONFIGURATION ICI ======
# ==============================

QB_HOST = "http://192.168.10.100:8080"
QB_USER = "mreclus"
QB_PASS = "MatMai172356!!"
VERIFY_SSL = False

OUTPUT_FILE = "qb_grouped.json"

# Inclure les torrents sans catégorie ?
INCLUDE_NO_CATEGORY = False

# ==============================
# ========= UTILITIES ==========
# ==============================

def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r'\[.*?\]|\(.*?\)', ' ', t)
    t = re.sub(r'\b(720p|1080p|2160p|4k|x264|x265|h264|h265|bluray|bdrip|web-dl|webrip|hdrip|dvdrip|aac|mp3|hdtv)\b', ' ', t)
    t = re.sub(r'[_\.\-]', ' ', t)
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def guess_info(name: str):
    try:
        g = guessit(name)
    except Exception:
        return {}
    return g


def is_season_pack(guess, torrent_name, file_paths):
    if guess.get("type") == "season":
        return True

    episode_list = guess.get("episode_list") or guess.get("episode_numbers")
    if episode_list and len(episode_list) >= 3:
        return True

    low = torrent_name.lower()
    if any(word in low for word in ["complete", "pack", "integrale", "full season", "saison complete"]):
        return True

    if guess.get("season") and not guess.get("episode"):
        count = 0
        for f in file_paths:
            if re.search(r'[sS]\d{1,2}[eE]\d{1,2}', f):
                count += 1
        if count >= 3:
            return True

    return False


# ==============================
# ===== QBITTORRENT CLIENT =====
# ==============================

class QBClient:
    def __init__(self):
        self.s = requests.Session()
        self.login()

    def login(self):
        r = self.s.post(
            f"{QB_HOST}/api/v2/auth/login",
            data={"username": QB_USER, "password": QB_PASS},
            verify=VERIFY_SSL
        )
        if r.status_code != 200 or "Ok" not in r.text:
            raise Exception("Erreur login qBittorrent")

    def get_torrents(self):
        r = self.s.get(f"{QB_HOST}/api/v2/torrents/info", verify=VERIFY_SSL)
        r.raise_for_status()
        return r.json()

    def get_files(self, info_hash):
        r = self.s.get(
            f"{QB_HOST}/api/v2/torrents/files?hash={info_hash}",
            verify=VERIFY_SSL
        )
        r.raise_for_status()
        return r.json()

    def get_trackers(self, info_hash):
        try:
            r = self.s.get(
                f"{QB_HOST}/api/v2/torrents/trackers?hash={info_hash}",
                verify=VERIFY_SSL
            )
            r.raise_for_status()
            return r.json()
        except:
            return []


# ==============================
# ========= MAIN LOGIC =========
# ==============================

def main():
    qb = QBClient()
    torrents = qb.get_torrents()

    films = defaultdict(list)
    series_anime = defaultdict(list)

    print("Analyse des torrents...")

    for t in torrents:
        category = (t.get("category") or "").lower()
        name = t.get("name")
        info_hash = t.get("hash")

        if not INCLUDE_NO_CATEGORY:
            if category not in ["film", "films", "movie", "series", "serie", "anime", "animes"]:
                continue

        files_raw = qb.get_files(info_hash)
        file_paths = [f.get("name") for f in files_raw]
        weight = sum(f.get("size", 0) for f in files_raw)

        trackers = qb.get_trackers(info_hash)
        indexer = trackers[0].get("url") if trackers else None

        guess = guess_info(name)
        title = guess.get("title") or guess.get("series") or guess.get("movie") or name
        title_norm = normalize_title(title)

        # ================= FILMS =================
        if category in ["film", "films", "movie"]:
            entry = {
                "torrent_name": name,
                "info_hash": info_hash,
                "indexer": indexer,
                "weight": weight
            }
            films[title_norm].append(entry)

        # ============ SERIES / ANIME ============
        else:
            season = guess.get("season")
            episode = guess.get("episode")

            season_pack = is_season_pack(guess, name, file_paths)

            entry = {
                "torrent_name": name,
                "info_hash": info_hash,
                "indexer": indexer,
                "weight": weight,
                "isSeasonPack": season_pack,
                "season": season,
                "episode": None if season_pack else episode
            }

            series_anime[title_norm].append(entry)

    result = {
        "films": dict(films),
        "series_anime": dict(series_anime)
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("Terminé.")
    print(f"Films détectés : {len(films)}")
    print(f"Séries/Animes détectés : {len(series_anime)}")
    print(f"Fichier généré : {OUTPUT_FILE}")


if __name__ == "__main__":
    main()