#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qb_count_precis_v2.py
Version améliorée :
 - vérifie la connexion (GET /api/v2/app/version) après login
 - options courtes: -H -U -P -n -c -s, plus --help automatique
 - imprime uniquement un entier sur stdout (sauf si --verbose)
 - messages d'erreur/status sur stderr
 - supporte env vars QBT_HOST / QBT_USER / QBT_PASS
"""
from __future__ import annotations
import os
import sys
import argparse
import requests
import re
from typing import Optional, List

# Default from environment if present
DEFAULT_HOST = os.environ.get('QBT_HOST', 'https://qbittorent.infra-prs.fr/')
DEFAULT_USER = os.environ.get('QBT_USER', 'mreclus')
DEFAULT_PASS = os.environ.get('QBT_PASS', '')

# Known API-level filter values (server-side /api/v2/torrents/info?filter=...)
API_FILTERS = {
    'all', 'downloading', 'seeding', 'completed', 'paused',
    'active', 'inactive', 'resumed', 'stalled',
    'stalled_uploading', 'stalled_downloading', 'errored'
}

# Examples of precise torrent['state'] values returned in JSON (doc-based examples)
EXAMPLE_STATES = [
    'pausedUP', 'pausedDL', 'queuedDL', 'queuedUP',
    'stalledDL', 'stalledUP', 'checkingDL', 'checkingUP',
    'missingFiles', 'uploading', 'downloading', 'error',
    'allocating', 'fetchingMetadata', 'unknown'
]

TIMEOUT = 10.0  # seconds for HTTP requests

def login(session: requests.Session, base_url: str, username: str, password: str, verify_ssl: bool=True) -> bool:
    """Login to qBittorrent WebAPI. Return True if SID cookie present."""
    login_url = base_url.rstrip('/') + '/api/v2/auth/login'
    headers = {'Referer': base_url}
    data = {'username': username, 'password': password}
    try:
        r = session.post(login_url, data=data, headers=headers, timeout=TIMEOUT, allow_redirects=False, verify=verify_ssl)
    except requests.RequestException as e:
        print(f"Erreur HTTP lors du login: {e}", file=sys.stderr)
        return False
    # success if 200 and SID cookie set
    if r.status_code == 200 and 'SID' in session.cookies.get_dict():
        return True
    # some installations may return 200 with empty body but still set cookie; above handles it
    # otherwise print reason for debugging
    print(f"Login échoué (HTTP {r.status_code}). Vérifie user/mdp et host.", file=sys.stderr)
    return False

def check_connection(session: requests.Session, base_url: str, verify_ssl: bool=True) -> bool:
    """Check connectivity by calling /api/v2/app/version (returns qBittorrent version)."""
    url = base_url.rstrip('/') + '/api/v2/app/version'
    try:
        r = session.get(url, timeout=TIMEOUT, verify=verify_ssl)
        if r.status_code == 200 and r.text.strip():
            # optionally parse but presence indicates connected
            return True
        else:
            print(f"Échec check_connection: HTTP {r.status_code}", file=sys.stderr)
            return False
    except requests.RequestException as e:
        print(f"Erreur check_connection: {e}", file=sys.stderr)
        return False

def fetch_torrents(session: requests.Session, base_url: str, api_filter: Optional[str]=None, category: Optional[str]=None, verify_ssl: bool=True) -> List[dict]:
    """GET /api/v2/torrents/info with optional filter and category."""
    url = base_url.rstrip('/') + '/api/v2/torrents/info'
    params = {}
    if api_filter:
        params['filter'] = api_filter
    if category:
        params['category'] = category
    r = session.get(url, params=params, timeout=TIMEOUT, verify=verify_ssl)
    r.raise_for_status()
    return r.json()

def matches_name(torrent: dict, name_pattern: Optional[str], use_regex: bool=False) -> bool:
    """Match by substring or regex. If no pattern -> True."""
    if not name_pattern:
        return True
    name = (torrent.get('name') or '')
    if use_regex:
        try:
            return re.search(name_pattern, name, re.IGNORECASE) is not None
        except re.error:
            # invalid regex -> treat as substring
            return name_pattern.lower() in name.lower()
    else:
        return name_pattern.lower() in name.lower()

def matches_state(torrent: dict, precise_state: Optional[str]) -> bool:
    """If no precise_state -> True. Otherwise compare torrent['state'] exactly."""
    if not precise_state:
        return True
    state = torrent.get('state')
    return state == precise_state

def parse_args():
    p = argparse.ArgumentParser(description='Compter les torrents qBittorrent selon filtres précis. '
                                           'Imprime uniquement un entier (sauf --verbose).')
    # credentials / host
    p.add_argument('-H', '--host', default=DEFAULT_HOST, help=f'Base URL WebUI (env QBT_HOST). ex: http://127.0.0.1:8080 (default: %(default)s)')
    p.add_argument('-U', '--user', default=DEFAULT_USER, help='WebUI username (env QBT_USER)')
    p.add_argument('-P', '--pass', dest='password', default=DEFAULT_PASS, help='WebUI password (env QBT_PASS)')
    # filters
    p.add_argument('-n', '--name', help='Substring to match in torrent name (case-insensitive)')
    p.add_argument('-c', '--category', help='Category (ex: films)')
    p.add_argument('-s', '--status', help=('Either an API filter ({}) or a precise torrent state (ex: missingFiles, pausedUP, stalledDL).'
                                           .format(','.join(sorted(API_FILTERS)))))
    p.add_argument('--regex', action='store_true', help='Treat --name as a regex (PCRE). If invalid regex, falls back to substring.')
    # misc
    p.add_argument('--no-verify-ssl', dest='verify_ssl', action='store_false', help='Disable SSL cert verification (useful for self-signed).')
    p.add_argument('-v', '--verbose', action='store_true', help='Verbose output to stderr (otherwise only integer printed to stdout).')
    return p.parse_args()

def main():
    args = parse_args()

    # decide if status is API filter or precise state
    api_filter = None
    precise_state = None
    if args.status:
        if args.status in API_FILTERS:
            api_filter = args.status
        else:
            precise_state = args.status

    s = requests.Session()
    # login
    if args.verbose:
        print("Tentative de connexion...", file=sys.stderr)
    ok = login(s, args.host, args.user, args.password, verify_ssl=args.verify_ssl)
    if not ok:
        print("0")  # numeric-only output for automation when failed
        if args.verbose:
            print("Connexion échouée.", file=sys.stderr)
        sys.exit(1)

    # connection check
    if args.verbose:
        print("Vérification de la connexion (appel /api/v2/app/version)...", file=sys.stderr)
    if not check_connection(s, args.host, verify_ssl=args.verify_ssl):
        print("0")
        if args.verbose:
            print("Vérification de la connexion échouée.", file=sys.stderr)
        sys.exit(2)

    # fetch torrents (server-side filter if api_filter provided)
    try:
        torrents = fetch_torrents(s, args.host, api_filter=api_filter, category=args.category, verify_ssl=args.verify_ssl)
    except Exception as e:
        print("0")
        if args.verbose:
            print(f"Erreur récupération torrents: {e}", file=sys.stderr)
        sys.exit(3)

    # local filtering: name + precise_state if provided
    matched = []
    for t in torrents:
        if not matches_name(t, args.name, use_regex=args.regex):
            continue
        if precise_state:
            if not matches_state(t, precise_state):
                continue
        matched.append(t)

    count = len(matched)
    # Print only the integer to stdout for automation
    print(count)

    # If verbose: print a short summary on stderr (not on stdout)
    if args.verbose:
        print(f"[VERBOSE] Connexion OK. Host={args.host} User={args.user}", file=sys.stderr)
        print(f"[VERBOSE] Filtres appliqués: api_filter={api_filter} precise_state={precise_state} category={args.category} name={args.name} regex={args.regex}", file=sys.stderr)
        print(f"[VERBOSE] Totaux: returned_by_api={len(torrents)} matched_locally={count}", file=sys.stderr)
        # optionally list names:
        for t in matched[:50]:
            print(f"[VERBOSE] - {t.get('name')} (state={t.get('state')})", file=sys.stderr)
        if len(matched) > 50:
            print(f"[VERBOSE] ...et {len(matched)-50} autres (tronqué).", file=sys.stderr)

if __name__ == '__main__':
    main()
