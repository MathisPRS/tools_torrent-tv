#!/usr/bin/env python3
# coding: utf-8
"""
add_crossseeds_from_qb.py

- Lit INPUT_JSON (films + series/anime groups with torrents)
- Se connecte à qBittorrent et récupère tous les torrents
- Cherche occurrences EXACTES de torrent_name dans qB (filtrées par QB_SEARCH_CATEGORIES)
- Ajoute cross_seed et normalise indexer comme demandé
- Écrit OUTPUT_JSON
"""

from __future__ import annotations
import requests, json, os, sys, time
from typing import Any, Dict, List, Optional

# -------- CONFIG --------
INPUT_JSON = "mycatalog_with_episodes.json"
OUTPUT_JSON = "mycatalog_with_crossseeds_qb.json"

QB_URL = "http://192.168.10.100:8080"      # ex: http://127.0.0.1:8080
QB_USERNAME = "mreclus"
QB_PASSWORD = "MatMai172356!!"

# qB categories to search for cross-seed candidates (exact string list)
QB_SEARCH_CATEGORIES = ["animes.cross", "cross-seed-link", "films.cross", "series.cross"]

# mapping partial host/domain -> friendly name
INDEXER_MAP = {
    "tracker.p2p-world.net": "ygg",
    "tracker.la-cale.space": "lacale",
    "tracker.torr9.xyz": "torr9",
    # add others here
}

# markers that indicate "DHT"/unknown - we will replace those with None or with found tracker if available
DHT_MARKERS = ["** [DHT] **", "dht", "p2p", "peer-to-peer"]

VERIFY_SSL = False
REQ_TIMEOUT = 30
API_SLEEP = 0.05   # small delay between qb requests if needed
# ------------------------

# ----- qB client -----
class QBClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.sess = requests.Session()
        self.username = username
        self.password = password
        self.logged = False

    def login(self):
        url = f"{self.base}/api/v2/auth/login"
        r = self.sess.post(url, data={"username": self.username, "password": self.password}, timeout=REQ_TIMEOUT, verify=VERIFY_SSL)
        if r.status_code == 200 and r.text.strip().lower().startswith("ok"):
            self.logged = True
            return True
        raise RuntimeError(f"qB login failed: {r.status_code} {r.text}")

    def list_torrents(self) -> List[Dict[str, Any]]:
        if not self.logged:
            self.login()
        url = f"{self.base}/api/v2/torrents/info"
        # retrieve all torrents
        r = self.sess.get(url, timeout=REQ_TIMEOUT, verify=VERIFY_SSL)
        r.raise_for_status()
        return r.json()

# ----- helpers -----
def normalize_indexer_from_string(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    for m in DHT_MARKERS:
        if m.lower() in low:
            return None
    # try substring mapping
    for sub, friendly in INDEXER_MAP.items():
        if sub in low:
            return friendly
    # try URL host extraction
    if "://" in low:
        try:
            host = low.split("://",1)[1].split("/",1)[0]
            host = host.split(":",1)[0]
            for sub, friendly in INDEXER_MAP.items():
                if sub in host:
                    return friendly
            return host
        except Exception:
            pass
    return s

def determine_indexer_from_qb_torrent(qbt: Dict[str, Any]) -> Optional[str]:
    """
    qB might expose 'tracker' or 'trackers' fields. Use them to determine a friendly indexer name.
    """
    # direct 'tracker' field
    if qbt.get("tracker"):
        return normalize_indexer_from_string(qbt.get("tracker"))
    # trackers list (sometimes qB returns 'trackers' list of dicts)
    trks = qbt.get("trackers") or qbt.get("tracker_list") or None
    if trks and isinstance(trks, list) and len(trks) > 0:
        first = trks[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("address") or None
            return normalize_indexer_from_string(url)
        else:
            return normalize_indexer_from_string(str(first))
    # fallback to 'added_by' or 'label' maybe
    if qbt.get("label"):
        return normalize_indexer_from_string(qbt.get("label"))
    return None

# ----- core processing -----
def collect_qb_map(qb: QBClient, allowed_categories: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Returns map: torrent_name -> list of qb-torrent-dicts
    Only includes qb torrents whose 'category' is in allowed_categories (if allowed_categories non-empty).
    """
    qb_torrents = qb.list_torrents()
    time.sleep(API_SLEEP)
    name_map: Dict[str, List[Dict[str, Any]]] = {}
    for t in qb_torrents:
        name = t.get("name")
        if not name:
            continue
        cat = t.get("category") or ""
        # if allowed_categories empty => include all, else filter exact match
        if allowed_categories and (cat not in allowed_categories):
            continue
        name_map.setdefault(name, []).append(t)
    return name_map

def add_crossseeds_by_qb(data: Dict[str, Any], qb_map: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Walk the data dict (groups of dict-of-lists) and for each torrent item:
     - set/update "indexer" from qB if possible
     - find qb_map[torrent_name] and create cross_seed entries {hash, category, torrent_name, indexer}
    """
    out = dict(data)  # shallow copy
    # iterate groups (films, series keys...)
    for group_key, group_val in list(data.items()):
        if not isinstance(group_val, dict):
            continue
        new_group: Dict[str, Any] = {}
        for parent_key, torrents in group_val.items():
            if not isinstance(torrents, list):
                new_group[parent_key] = torrents
                continue
            new_torrents = []
            for t in torrents:
                tcopy = dict(t)
                tn = tcopy.get("torrent_name")
                ih = tcopy.get("info_hash")
                # try to find matching qb entries by exact torrent name
                qb_matches = qb_map.get(tn) or []
                # update indexer from qb if available (prefer first qb match)
                if qb_matches:
                    qb_first = qb_matches[0]
                    idx = determine_indexer_from_qb_torrent(qb_first)
                    if idx:
                        tcopy["indexer"] = idx
                else:
                    # if local indexer is DHT-like, normalize to None
                    idx_local = tcopy.get("indexer")
                    norm = normalize_indexer_from_string(idx_local)
                    tcopy["indexer"] = norm

                # build cross_seed list from qb_matches (exclude same hash)
                cs = []
                seen = set()
                for qm in qb_matches:
                    qh = qm.get("hash") or qm.get("info_hash") or qm.get("torrent_hash")
                    if not qh:
                        continue
                    if ih and str(qh).lower() == str(ih).lower():
                        # skip exact same hash — unless you want to include duplicates from different category; skip for now
                        continue
                    qname = qm.get("name")
                    qcat = qm.get("category") or ""
                    qidx = determine_indexer_from_qb_torrent(qm)
                    key = (qh, qcat, qname)
                    if key in seen:
                        continue
                    seen.add(key)
                    cs.append({
                        "hash": qh,
                        "category": qcat,
                        "torrent_name": qname,
                        "indexer": qidx
                    })
                # also consider matches within local JSON other groups: we will add those later by merging results across data,
                # but since we specifically requested qb search, we rely on qb_map results.
                tcopy["cross_seed"] = cs
                new_torrents.append(tcopy)
            new_group[parent_key] = new_torrents
        out[group_key] = new_group
    return out

# ----- main -----
def main():
    if not os.path.isfile(INPUT_JSON):
        print(f"[!] Input JSON not found: {INPUT_JSON}", file=sys.stderr); sys.exit(2)
    with open(INPUT_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    qb = QBClient(QB_URL, QB_USERNAME, QB_PASSWORD)
    try:
        qb.login()
    except Exception as e:
        print(f"[!] qB login failed: {e}", file=sys.stderr); sys.exit(1)

    qb_map = collect_qb_map(qb, QB_SEARCH_CATEGORIES)
    print(f"[+] qB mapa construit: {sum(len(v) for v in qb_map.values())} torrents indexés dans {len(qb_map)} noms")

    enriched = add_crossseeds_by_qb(data, qb_map)

    # Finally, also dedupe cross_seed lists and ensure indexer normalized for all items
    for group_key, group_val in enriched.items():
        if not isinstance(group_val, dict):
            continue
        for parent_key, torrents in group_val.items():
            if not isinstance(torrents, list):
                continue
            for t in torrents:
                # normalize indexer
                t["indexer"] = normalize_indexer_from_string(t.get("indexer"))
                # dedupe cross_seed by hash+category+name
                cs = t.get("cross_seed") or []
                seen = set(); out_cs = []
                for c in cs:
                    key = (str(c.get("hash")), str(c.get("category")), str(c.get("torrent_name")))
                    if key in seen: continue
                    seen.add(key)
                    # normalize c.indexer
                    c["indexer"] = normalize_indexer_from_string(c.get("indexer"))
                    out_cs.append(c)
                t["cross_seed"] = out_cs

    # write output
    try:
        with open(OUTPUT_JSON, "w", encoding="utf-8") as fh:
            json.dump(enriched, fh, indent=2, ensure_ascii=False)
        print(f"[+] Wrote enriched file -> {OUTPUT_JSON}")
    except Exception as e:
        print(f"[!] Error writing output: {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()