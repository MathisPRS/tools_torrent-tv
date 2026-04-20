#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
from typing import List, Dict, Any

# ==============================
# ===== CONFIGURATION ==========
# ==============================

# Sonarr
SONARR_HOST = "http://192.168.10.100:8989"   # ex: http://sonarr.local:8989
SONARR_API_KEY = "c38a95cb00cb49f7b49d478e77b994f5"

# Radarr
RADARR_HOST = "http://192.168.10.100:7878"   # ex: http://radarr.local:7878
RADARR_API_KEY = "bf545bb0412540de9d0a6b3cba6cecb1"

VERIFY_SSL = False

# Sortie
OUT_TXT = "catalog.txt"

# ==============================
# ===== HTTP helpers ===========
# ==============================

def request_get(url: str, params: Dict[str, Any] = None) -> Any:
    r = requests.get(url, params=params or {}, verify=VERIFY_SSL, timeout=30)
    r.raise_for_status()
    return r.json()

# ==============================
# ===== RADARR MOVIES ==========
# ==============================

def get_radarr_movies() -> List[Dict[str, Any]]:
    url = f"{RADARR_HOST}/api/v3/movie"
    params = {"apikey": RADARR_API_KEY}
    movies = request_get(url, params=params)

    downloaded = []
    for m in movies:
        if m.get("hasFile"):
            downloaded.append(m)
    # trier par titre principal
    downloaded.sort(key=lambda x: x.get("title","").lower())
    return downloaded

def get_radarr_alternate_titles(movie: Dict[str, Any]) -> List[str]:
    """
    Tente d'extraire les noms alternatifs pour un film.
    1) Cherche des champs dans l'objet lui-même (alternateTitles / alternativeTitles / titleSlug / sortTitle)
    2) Essaie l'endpoint /api/v3/movie/{id}/alternateTitles si disponible
    """
    alts = []

    # champs dans l'objet
    for key in ("alternateTitles", "alternativeTitles", "alternativeTitles", "alternativeTitle"):
        if movie.get(key):
            # peut être une liste d'objets ou de strings
            v = movie.get(key)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        # certains retours ont {title: "..."} ou {name: "..."}
                        if it.get("title"):
                            alts.append(it.get("title"))
                        elif it.get("name"):
                            alts.append(it.get("name"))
                        else:
                            # stringify fallback
                            alts.append(str(it))
                    else:
                        alts.append(str(it))
            elif isinstance(v, str):
                alts.append(v)

    # fallback: try endpoint /movie/{id}/alternateTitles
    if not alts and movie.get("id"):
        try:
            url = f"{RADARR_HOST}/api/v3/movie/{movie.get('id')}/alternateTitles"
            params = {"apikey": RADARR_API_KEY}
            resp = request_get(url, params=params)
            # resp is often a list of objects with 'title' or 'name'
            if isinstance(resp, list):
                for it in resp:
                    if isinstance(it, dict):
                        if it.get("title"):
                            alts.append(it.get("title"))
                        elif it.get("name"):
                            alts.append(it.get("name"))
                        else:
                            alts.append(str(it))
                    else:
                        alts.append(str(it))
        except requests.exceptions.HTTPError:
            # endpoint not present or not allowed -> ignore silently
            pass
        except Exception:
            pass

    # other possible single-field fallbacks
    if movie.get("titleSlug"):
        alts.append(movie.get("titleSlug"))
    if movie.get("sortTitle"):
        alts.append(movie.get("sortTitle"))

    # uniq and keep order
    seen = set()
    out = []
    for a in alts:
        if not a:
            continue
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out

# ==============================
# ===== SONARR SERIES ==========
# ==============================

def get_sonarr_series() -> List[Dict[str, Any]]:
    url = f"{SONARR_HOST}/api/v3/series"
    params = {"apikey": SONARR_API_KEY}
    series = request_get(url, params=params)

    downloaded = []
    for s in series:
        if s.get("statistics", {}).get("episodeFileCount", 0) > 0:
            downloaded.append(s)
    downloaded.sort(key=lambda x: x.get("title","").lower())
    return downloaded

def get_sonarr_alternate_titles(series: Dict[str, Any]) -> List[str]:
    """
    Tente d'extraire les noms alternatifs pour une série.
    1) Cherche dans l'objet (alternateTitles / alternativeTitles / cleanTitle / sortTitle / titleSlug)
    2) Essaie endpoint /series/{id}/alternateTitles
    """
    alts = []

    # champs directement dans l'objet
    # Sonarr peut exposer: "alternativeTitles" or "titleSlug" or "cleanTitle", "sortTitle"
    for key in ("alternativeTitles", "alternateTitles", "alternateTitle", "alternativeTitle"):
        if series.get(key):
            v = series.get(key)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        if it.get("title"):
                            alts.append(it.get("title"))
                        elif it.get("name"):
                            alts.append(it.get("name"))
                        else:
                            alts.append(str(it))
                    else:
                        alts.append(str(it))
            elif isinstance(v, str):
                alts.append(v)

    # other helpful fields
    for key in ("cleanTitle", "sortTitle", "titleSlug"):
        if series.get(key):
            alts.append(series.get(key))

    # try endpoint /series/{id}/alternateTitles if nothing found
    if not alts and series.get("id"):
        try:
            url = f"{SONARR_HOST}/api/v3/series/{series.get('id')}/alternateTitles"
            params = {"apikey": SONARR_API_KEY}
            resp = request_get(url, params=params)
            # expect list
            if isinstance(resp, list):
                for it in resp:
                    if isinstance(it, dict):
                        if it.get("title"):
                            alts.append(it.get("title"))
                        elif it.get("name"):
                            alts.append(it.get("name"))
                        else:
                            alts.append(str(it))
                    else:
                        alts.append(str(it))
        except requests.exceptions.HTTPError:
            # endpoint may not exist on older Sonarr builds -> ignore
            pass
        except Exception:
            pass

    # uniq preserve order
    seen = set()
    out = []
    for a in alts:
        if not a:
            continue
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out

# ==============================
# =========== MAIN =============
# ==============================

def main():
    # récupère data
    print("Récupération des films Radarr...")
    movies = get_radarr_movies()
    print("Récupération des séries Sonarr...")
    series = get_sonarr_series()

    # préparer lignes à écrire
    lines = []
    lines.append("FILMS:")
    for m in movies:
        title = m.get("title", "(unknown)")
        alts = get_radarr_alternate_titles(m)
        if alts:
            lines.append(f"{title} | " + " ; ".join(alts))
        else:
            lines.append(f"{title} | (aucun alternatif)")

    lines.append("")  # blank line
    lines.append("SERIES:")
    for s in series:
        title = s.get("title", "(unknown)")
        alts = get_sonarr_alternate_titles(s)
        if alts:
            lines.append(f"{title} | " + " ; ".join(alts))
        else:
            lines.append(f"{title} | (aucun alternatif)")

    # write txt file
    out_path = OUT_TXT
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
        print(f"Fichier écrit -> {out_path}")
    except Exception as e:
        print("Erreur écriture fichier:", e)

if __name__ == "__main__":
    main()