#!/usr/bin/env python3
"""
sonarr_storage_report.py

But : comparer l'espace disque utilisé par série *en fonction du nombre d'épisodes téléchargés*,
et produire un classement trié du plus gourmand (moyenne bytes/épisode) au moins gourmand.

Usage:
    python sonarr_storage_report.py --url http://localhost:8989 --api-key YOUR_KEY --out csv
    python sonarr_storage_report.py --url http://sonarr.local:8989 --api-key KEY --limit 50

Sortie: print console + option --out csv/json
"""
import argparse
import requests
import csv
import json
from pathlib import Path

def bytes_to_human(n):
    # simple human readable
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if abs(n) < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PiB"

def get(api_base, api_key, path, params=None):
    headers = {'X-Api-Key': api_key}
    url = api_base.rstrip('/') + '/api/v3/' + path.lstrip('/')
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def build_report(api_base, api_key, series_filter=None, verbose=False):
    series_list = get(api_base, api_key, 'series')
    report = []
    for s in series_list:
        sid = s.get('id')
        title = s.get('title') or s.get('seriesName') or s.get('title')
        if series_filter and series_filter.lower() not in title.lower():
            continue

        # 1) get episode files for this series (unique physical files)
        # endpoint: /api/episodefile?seriesId={sid}
        try:
            episode_files = get(api_base, api_key, 'episodefile', params={'seriesId': sid})
        except requests.HTTPError as e:
            # some Sonarr builds use 'episodefiles' plural; try both
            try:
                episode_files = get(api_base, api_key, 'episodefiles', params={'seriesId': sid})
            except Exception:
                if verbose:
                    print(f"Erreur récupération episode files for {title} ({sid}): {e}")
                episode_files = []

        total_bytes = 0
        seen_file_ids = set()
        for ef in episode_files:
            # ef is expected to contain 'id' and 'size' (bytes)
            fid = ef.get('id') or ef.get('episodeFileId') or ef.get('fileId')
            if fid is None:
                # fallback: try path as unique id
                fid = ef.get('path')
            if fid in seen_file_ids:
                continue
            seen_file_ids.add(fid)
            size = ef.get('size') or ef.get('sizeOnDisk') or 0
            # some older endpoints or wrappers might provide strings
            try:
                size = int(size)
            except Exception:
                size = 0
            total_bytes += size

        # 2) count downloaded episodes (hasFile true)
        try:
            episodes = get(api_base, api_key, 'episode', params={'seriesId': sid})
        except Exception:
            # fallback: episodes endpoint might require no params and filter locally
            try:
                all_eps = get(api_base, api_key, 'episode')
                episodes = [e for e in all_eps if e.get('seriesId') == sid]
            except Exception:
                episodes = []

        downloaded_eps = 0
        for ep in episodes:
            # Sonarr episode object: 'hasFile' boolean
            if ep.get('hasFile'):
                downloaded_eps += 1

        avg_per_episode = (total_bytes / downloaded_eps) if downloaded_eps > 0 else 0

        report.append({
            'seriesId': sid,
            'title': title,
            'total_bytes': total_bytes,
            'downloaded_episodes': downloaded_eps,
            'avg_bytes_per_episode': int(avg_per_episode),
            'sizeOnDisk_series_field': s.get('sizeOnDisk', None),  # for cross-check
            'path': s.get('path') or s.get('rootFolderPath')
        })

    # sort by avg bytes per episode desc (pire -> meilleur)
    report.sort(key=lambda x: x['avg_bytes_per_episode'], reverse=True)
    return report

def print_report(report, top=None):
    rows = report if top is None else report[:top]
    print(f"{'Rank':>4} | {'Series':40} | {'#Eps':>5} | {'Total':>10} | {'Avg/ep':>10}")
    print("-"*90)
    for i, r in enumerate(rows, 1):
        print(f"{i:>4} | {r['title'][:40]:40} | {r['downloaded_episodes']:>5} | {bytes_to_human(r['total_bytes']):>10} | {bytes_to_human(r['avg_bytes_per_episode']):>10}")

def export_csv(report, outpath):
    keys = ['seriesId','title','downloaded_episodes','total_bytes','avg_bytes_per_episode','sizeOnDisk_series_field','path']
    with open(outpath, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in report:
            w.writerow(r)

def export_json(report, outpath):
    with open(outpath, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

def main():
    p = argparse.ArgumentParser(description="Sonarr storage per-episode report")
    p.add_argument('--url', required=True, help='Base URL of Sonarr (e.g. http://sonarr.local:8989)')
    p.add_argument('--api-key', required=True, help='Sonarr API key (X-Api-Key)')
    p.add_argument('--out', choices=['csv','json','none'], default='none', help='Exporter en csv/json')
    p.add_argument('--out-file', help='Chemin du fichier de sortie (par défaut report.csv/report.json)')
    p.add_argument('--top', type=int, help='Afficher seulement les N premiers')
    p.add_argument('--filter', help='Filtrer les séries par substring dans le titre')
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()

    report = build_report(args.url, args.api_key, series_filter=args.filter, verbose=args.verbose)
    print_report(report, top=args.top)

    if args.out != 'none':
        out_file = args.out_file or ('sonarr_storage_report.' + args.out)
        if args.out == 'csv':
            export_csv(report, out_file)
        else:
            export_json(report, out_file)
        print(f"\nExporté -> {Path(out_file).absolute()}")

if __name__ == '__main__':
    main()
