#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search_qb.py
Amélioration : filtering par state (précis ou friendly), tags, category, name (substring/regex).
Imprime seulement un entier sur stdout (le nombre de torrents correspondants).
Messages d'info/erreur s'affichent sur stderr si -v/--verbose.

Dépendance: requests
"""
from __future__ import annotations
import os, sys, re, argparse
from typing import List, Optional
import requests

# Defaults from env (modifiable)
DEFAULT_HOST = os.environ.get('QBT_HOST', 'http://127.0.0.1:8080')
DEFAULT_USER = os.environ.get('QBT_USER', 'mreclus')
DEFAULT_PASS = os.environ.get('QBT_PASS', '')

TIMEOUT = 10.0

# Server-side API filter values (documented)
API_FILTERS = {
    'all','downloading','seeding','completed','paused','active','inactive',
    'resumed','stalled','stalled_uploading','stalled_downloading','errored'
}

# Map "friendly" status terms -> matcher function or set of candidate state substrings.
# The actual torrent['state'] values are like 'StoppedUP','StoppedDL','stalledDL','pausedUP','missingFiles', etc.
# See qBittorrent docs for exact state names. :contentReference[oaicite:3]{index=3}
FRIENDLY_STATUS_MAP = {
    'stopped': lambda s: s and ('stopped' in s.lower() or 'paused' in s.lower()),
    'paused':  lambda s: s and 'paused' in s.lower(),
    'stalled': lambda s: s and 'stalled' in s.lower(),
    'missing': lambda s: s and 'missing' in s.lower(),       # matches missingFiles
    'checking':lambda s: s and 'checking' in s.lower(),
    'downloading': lambda s: s and 'downloading' in s.lower(),
    'uploading': lambda s: s and 'uploading' in s.lower(),
    'error':   lambda s: s and ('error' in s.lower() or s.lower()=='error'),
    # fallback: exact match handled separately
}

def login(session: requests.Session, base_url: str, user: str, password: str, verify_ssl: bool=True) -> bool:
    url = base_url.rstrip('/') + '/api/v2/auth/login'
    headers = {'Referer': base_url}
    try:
        r = session.post(url, data={'username': user, 'password': password}, headers=headers,
                         timeout=TIMEOUT, allow_redirects=False, verify=verify_ssl)
    except requests.RequestException as e:
        print(f"[ERR] HTTP login error: {e}", file=sys.stderr)
        return False
    if r.status_code == 200 and 'SID' in session.cookies.get_dict():
        return True
    # if 200 but no cookie, consider failed
    print(f"[ERR] Login failed (HTTP {r.status_code}).", file=sys.stderr)
    return False

def check_connection(session: requests.Session, base_url: str, verify_ssl: bool=True) -> bool:
    url = base_url.rstrip('/') + '/api/v2/app/version'
    try:
        r = session.get(url, timeout=TIMEOUT, verify=verify_ssl)
        return r.status_code == 200 and bool(r.text.strip())
    except requests.RequestException as e:
        print(f"[ERR] Connection check failed: {e}", file=sys.stderr)
        return False

def fetch_torrents(session: requests.Session, base_url: str, api_filter: Optional[str]=None,
                   category: Optional[str]=None, tag: Optional[str]=None, verify_ssl: bool=True) -> List[dict]:
    """
    Call /api/v2/torrents/info. The WebAPI supports filter & category (server side),
    and some clients/CLI also support tag param. We send filter & category; tag filtering
    will be done client-side (some endpoints support 'tag' as query param depending on version).
    """
    url = base_url.rstrip('/') + '/api/v2/torrents/info'
    params = {}
    if api_filter:
        params['filter'] = api_filter
    if category:
        params['category'] = category
    # NOTE: older/newer qBittorrent versions may or may not accept 'tag' as query param;
    # to be portable, we always fetch and filter tags locally. (API docs show tag endpoints exist). :contentReference[oaicite:4]{index=4}
    r = session.get(url, params=params, timeout=TIMEOUT, verify=verify_ssl)
    r.raise_for_status()
    return r.json()

def parse_tags_field(tags_field: Optional[str]) -> List[str]:
    """
    qBittorrent returns tags as a string (comma-separated) or empty string;
    normalize to list of lowercased tag names.
    """
    if not tags_field:
        return []
    # tags may be like "films,HD", or "films" or ""
    parts = [p.strip().lower() for p in tags_field.split(',') if p.strip()]
    return parts

def matches_tag(torrent: dict, tag_query: Optional[str]) -> bool:
    if not tag_query:
        return True
    # accept multiple tags separated by comma in tag_query -> logical OR (match at least one)
    wanted = [t.strip().lower() for t in tag_query.split(',') if t.strip()]
    t_tags = parse_tags_field(torrent.get('tags', ''))
    # match if any wanted tag in torrent tags
    for w in wanted:
        if w in t_tags:
            return True
    return False

def matches_name(torrent: dict, name_query: Optional[str], use_regex: bool=False) -> bool:
    if not name_query:
        return True
    name = torrent.get('name', '') or ''
    if use_regex:
        try:
            return re.search(name_query, name, re.IGNORECASE) is not None
        except re.error:
            # invalid regex -> fallback substring
            return name_query.lower() in name.lower()
    else:
        return name_query.lower() in name.lower()

def matches_state(torrent: dict, status_arg: Optional[str]) -> bool:
    """
    status_arg handling:
     - if None -> True
     - if in API_FILTERS we assume server filter applied (but to be safe we accept everything here)
     - if equal one of exact states (like 'missingFiles','StoppedUP',...) -> exact compare (case-sensitive typical)
     - if friendly term (stopped/paused/stalled/...) -> use FRIENDLY_STATUS_MAP match on torrent['state'] (case-insensitive)
    See qBittorrent state names in docs. :contentReference[oaicite:5]{index=5}
    """
    if not status_arg:
        return True
    # If user provided an API filter name, we rely on server-side; but still return True here since server already filtered.
    if status_arg in API_FILTERS:
        return True
    # exact match (allow same-case or different-case)
    state = torrent.get('state') or ''
    if state == status_arg or state.lower() == status_arg.lower():
        return True
    # friendly synonyms
    f = FRIENDLY_STATUS_MAP.get(status_arg.lower())
    if f:
        return f(state)
    # fallback: if status_arg looks like 'missingFiles' vs 'missing files' accept both
    if status_arg.lower().replace(' ', '') == 'missingfiles' and 'missing' in state.lower():
        return True
    # if user asked 'stopped' but actual state 'pausedDL' older versions might use paused*, so check that
    if status_arg.lower() == 'stopped' and ('stopped' in state.lower() or 'paused' in state.lower()):
        return True
    # no match
    return False

def parse_args():
    p = argparse.ArgumentParser(description='Compter les torrents qBittorrent selon filtres précis. Imprime uniquement un entier (stdout).')
    p.add_argument('-H','--host', default=DEFAULT_HOST, help='Host WebUI (env QBT_HOST). ex: http://127.0.0.1:8080')
    p.add_argument('-U','--user', default=DEFAULT_USER, help='WebUI user (env QBT_USER)')
    p.add_argument('-P','--pass', dest='password', default=DEFAULT_PASS, help='WebUI password (env QBT_PASS)')
    p.add_argument('-n','--name', help='Substring or regex (with --regex) to match torrent name')
    p.add_argument('-c','--category', help='Category (ex: films)')
    p.add_argument('-t','--tag', help='Tag or comma-separated tags (ex: films,hd). Matches if torrent has at least one.')
    p.add_argument('-s','--status', help=('API filter (server-side) or precise state or friendly term (stopped, paused, stalled, missing, checking, downloading, uploading, error).'
                                          f' API filters: {",".join(sorted(API_FILTERS))}'))
    p.add_argument('--regex', action='store_true', help='Treat --name as regex (PCRE). If invalid regex, falls back to substring.')
    p.add_argument('--no-verify-ssl', dest='verify_ssl', action='store_false', help='Disable SSL verification (self-signed).')
    p.add_argument('-v','--verbose', action='store_true', help='Verbose logging to stderr.')
    return p.parse_args()

def main():
    args = parse_args()

    api_filter = None
    precise_state = None
    if args.status:
        if args.status in API_FILTERS:
            api_filter = args.status
        else:
            precise_state = args.status

    s = requests.Session()
    if args.verbose:
        print(f"[VERB] Login {args.user}@{args.host} ...", file=sys.stderr)
    if not login(s, args.host, args.user, args.password, verify_ssl=args.verify_ssl):
        # print numeric-only 0 on stdout for automation
        print("0")
        if args.verbose:
            print("[VERB] Login failed.", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print("[VERB] Checking connection (GET /api/v2/app/version)...", file=sys.stderr)
    if not check_connection(s, args.host, verify_ssl=args.verify_ssl):
        print("0")
        if args.verbose:
            print("[VERB] Connection check failed.", file=sys.stderr)
        sys.exit(2)

    try:
        torrents = fetch_torrents(s, args.host, api_filter=api_filter, category=args.category, tag=None, verify_ssl=args.verify_ssl)
    except Exception as e:
        if args.verbose:
            print(f"[ERR] fetch_torrents error: {e}", file=sys.stderr)
        print("0")
        sys.exit(3)

    matched = []
    for t in torrents:
        if not matches_name(t, args.name, use_regex=args.regex):
            continue
        if not matches_tag(t, args.tag):
            continue
        if not matches_state(t, precise_state):
            continue
        matched.append(t)

    # output only the integer count on stdout
    print(len(matched))

    # verbose: print details to stderr
    if args.verbose:
        print(f"[VERB] total returned_by_api={len(torrents)} matched_locally={len(matched)}", file=sys.stderr)
        print(f"[VERB] filters: api_filter={api_filter} precise_state={precise_state} category={args.category} tag={args.tag} name={args.name} regex={args.regex}", file=sys.stderr)
        for t in matched[:50]:
            print(f"[VERB] - {t.get('name')} (state={t.get('state')} tags={t.get('tags')})", file=sys.stderr)
        if len(matched) > 50:
            print(f"[VERB] ...and {len(matched)-50} more (truncated).", file=sys.stderr)

if __name__ == '__main__':
    main()
