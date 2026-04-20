#!/usr/bin/env python3
"""
Récupère les torrents qBittorrent pour les catégories films, animes, series
Renvoie un JSON structuré { "films": [...], "animes": [...], "series": [...] }
Chaque torrent contient au minimum: name, hash (ou hashes) et optionally other fields.
"""

import requests
import json
from typing import List, Dict

# ---------- VARIABLES DE CONNEXION (modifie ici) ----------
QB_HOST = "192.168.10.100"   # adresse IP ou hostname du serveur qbittorrent
QB_PORT = 8080          # port web UI (par défaut 8080)
QB_USERNAME = "mreclus"   # ton username qBittorrent
QB_PASSWORD = "MatMai172356!!" # ton mot de passe qBittorrent
USE_SSL = False         # True si tu utilises https sur le web UI
VERIFY_SSL = True       # False si certificat auto-signé (utile pour debug)
OUTPUT_FILE = "qb_torrents.json"
# -------------------------------------------

BASE_SCHEME = "https" if USE_SSL else "http"
BASE_URL = f"{BASE_SCHEME}://{QB_HOST}:{QB_PORT}"

CATEGORIES = ["films", "animes", "series"]


def login(session):
    url = f"{BASE_URL}/api/v2/auth/login"
    r = session.post(
        url,
        data={"username": QB_USERNAME, "password": QB_PASSWORD},
        verify=VERIFY_SSL,
        timeout=10
    )
    if r.status_code != 200:
        raise Exception("Erreur de connexion à qBittorrent")


def get_torrents(session, category):
    url = f"{BASE_URL}/api/v2/torrents/info"
    r = session.get(
        url,
        params={"category": category},
        verify=VERIFY_SSL,
        timeout=10
    )
    if r.status_code != 200:
        raise Exception(f"Erreur récupération catégorie {category}")

    torrents = r.json()

    return [
        {
            "name": t.get("name"),
            "hash": t.get("hash")
        }
        for t in torrents
    ]


def main():
    session = requests.Session()

    login(session)

    result = {}

    for category in CATEGORIES:
        result[category] = get_torrents(session, category)

    # Écriture dans le fichier JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Export terminé dans {OUTPUT_FILE}")


if __name__ == "__main__":
    main()