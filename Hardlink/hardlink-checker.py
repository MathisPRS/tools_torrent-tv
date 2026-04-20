#!/usr/bin/env python3
"""
hardlink-checker.py
-------------------
Scanne des dossiers de téléchargements (animes, films, series) et détecte
les fichiers/dossiers dont les hardlinks côté Sonarr/Radarr ont été supprimés.

Logique hardlink :
  nlink == 1  → orphelin  : le hardlink media a été supprimé, le torrent seed seul
  nlink >= 2  → sain      : le hardlink media est encore actif

Statuts d'une entrée (fichier seul ou dossier torrent) :
  ORPHAN             → tous les fichiers media ont nlink == 1  → supprimable
  MIXED              → certains nlink==1, d'autres nlink>=2    → non supprimable auto
  MIXED-WHITELISTED  → MIXED connu et validé manuellement      → ignoré silencieusement
  QUEUED             → hash présent dans la queue Sonarr/Radarr → jamais supprimé
  OK                 → tous les fichiers media ont nlink >= 2
  EMPTY              → aucun fichier media trouvé

Cas MIXED→ORPHAN : si une entrée whitelistée devient ORPHAN, elle est supprimée
automatiquement et retirée de la whitelist.

Usage :
  python hardlink-checker.py                        rapport + prompts whitelist
  python hardlink-checker.py --delete               rapport + suppression
  python hardlink-checker.py --delete --force       suppression sans confirmation
  python hardlink-checker.py --no-interactive       rapport seul sans prompts (cron)
  python hardlink-checker.py --show-ok              afficher aussi les entrées saines
  python hardlink-checker.py --config /path/to/cfg  config personnalisée
"""

import os
import sys
import json
import shutil
import logging
import argparse
import configparser
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ==============================================================================
# Constantes
# ==============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH    = os.path.join(SCRIPT_DIR, "config.cfg")
DEFAULT_WHITELIST_PATH = os.path.join(SCRIPT_DIR, "whitelist.json")

DEFAULT_MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".iso"
})

ARR_QUEUE_PAGE_SIZE = 500
ARR_QUEUE_TIMEOUT_S = 5

STATUS_ORPHAN     = "ORPHAN"
STATUS_MIXED      = "MIXED"
STATUS_MIXED_WL   = "MIXED-WHITELISTED"
STATUS_QUEUED     = "QUEUED"
STATUS_OK         = "OK"
STATUS_EMPTY      = "EMPTY"


# ==============================================================================
# Section CONFIG
# ==============================================================================

@dataclass
class AppConfig:
    """Toute la configuration de l'application en un objet typé."""
    category_paths:    dict[str, str]
    media_extensions:  frozenset[str]
    whitelist_path:    str
    sonarr_url:        Optional[str]
    sonarr_api_key:    Optional[str]
    radarr_url:        Optional[str]
    radarr_api_key:    Optional[str]
    show_ok:           bool
    no_interactive:    bool
    log_level:         str


def _parse_paths(cfg: configparser.ConfigParser) -> dict[str, str]:
    if not cfg.has_section("paths"):
        logging.error("Section [paths] manquante dans la configuration.")
        sys.exit(1)
    paths = {k.strip(): v.strip() for k, v in cfg.items("paths")}
    if not paths:
        logging.error("Aucun chemin défini dans [paths].")
        sys.exit(1)
    return paths


def _parse_media_extensions(cfg: configparser.ConfigParser) -> frozenset[str]:
    if not cfg.has_option("options", "media_extensions"):
        return DEFAULT_MEDIA_EXTENSIONS
    raw = cfg.get("options", "media_extensions")
    return frozenset(ext.strip().lower() for ext in raw.split(",") if ext.strip())


def _parse_arr_section(
    cfg: configparser.ConfigParser, section: str
) -> tuple[Optional[str], Optional[str]]:
    """Retourne (url, api_key) pour une section arr, ou (None, None) si absente/incomplète."""
    if not cfg.has_section(section):
        return None, None
    url     = cfg.get(section, "url",     fallback="").strip() or None
    api_key = cfg.get(section, "api_key", fallback="").strip() or None
    if not url or not api_key:
        if url or api_key:
            logging.warning(
                f"Section [{section}] incomplète (url et api_key requis). "
                "Queue guard désactivé pour cette source."
            )
        return None, None
    return url, api_key


def _parse_options(cfg: configparser.ConfigParser) -> tuple[bool, bool, str, str]:
    """Retourne (show_ok, no_interactive, log_level, whitelist_path)."""
    def get_bool(key: str, default: bool) -> bool:
        val = cfg.get("options", key, fallback="").strip().lower()
        return val in ("true", "1", "yes") if val else default

    show_ok        = get_bool("show_ok",        default=False)
    no_interactive = get_bool("no_interactive",  default=False)
    log_level      = cfg.get("options", "log_level", fallback="INFO").strip().upper()
    whitelist_path = cfg.get("options", "whitelist_path", fallback="").strip()
    if not whitelist_path:
        whitelist_path = DEFAULT_WHITELIST_PATH
    return show_ok, no_interactive, log_level, whitelist_path


def load_config(config_path: str) -> AppConfig:
    """Charge le fichier .cfg et retourne un AppConfig typé."""
    if not os.path.isfile(config_path):
        logging.error(f"Fichier de configuration introuvable : {config_path}")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(config_path)

    show_ok, no_interactive, log_level, whitelist_path = _parse_options(cfg)
    sonarr_url, sonarr_key = _parse_arr_section(cfg, "sonarr")
    radarr_url, radarr_key = _parse_arr_section(cfg, "radarr")

    return AppConfig(
        category_paths   = _parse_paths(cfg),
        media_extensions = _parse_media_extensions(cfg),
        whitelist_path   = whitelist_path,
        sonarr_url       = sonarr_url,
        sonarr_api_key   = sonarr_key,
        radarr_url       = radarr_url,
        radarr_api_key   = radarr_key,
        show_ok          = show_ok,
        no_interactive   = no_interactive,
        log_level        = log_level,
    )


# ==============================================================================
# Section ARR QUEUE GUARD
# ==============================================================================

def _build_arr_queue_url(base_url: str, unknown_items_param: str) -> str:
    return (
        f"{base_url.rstrip('/')}/api/v3/queue"
        f"?pageSize={ARR_QUEUE_PAGE_SIZE}"
        f"&{unknown_items_param}=true"
    )


def _fetch_arr_queue(base_url: str, api_key: str, unknown_items_param: str) -> set[str]:
    """
    Appelle GET /api/v3/queue sur une instance arr.
    Retourne les downloadId (hashes torrents) en minuscules.
    Retourne set() en cas d'erreur réseau ou de réponse invalide.
    """
    url = _build_arr_queue_url(base_url, unknown_items_param)
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=ARR_QUEUE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logging.warning(f"Impossible de joindre {base_url} : {e}")
        return set()
    except (json.JSONDecodeError, KeyError) as e:
        logging.warning(f"Réponse inattendue de {base_url} : {e}")
        return set()

    records = data.get("records", [])
    hashes = set()
    for record in records:
        download_id = record.get("downloadId", "")
        if download_id:
            hashes.add(download_id.strip().lower())
    return hashes


def get_queued_hashes(cfg: AppConfig) -> set[str]:
    """
    Agrège les hashes en queue de Sonarr et Radarr.
    Si une source est absente ou en erreur, continue avec l'autre.
    Retourne set() si aucune API n'est configurée.
    """
    all_hashes: set[str] = set()

    if cfg.sonarr_url and cfg.sonarr_api_key:
        sonarr_hashes = _fetch_arr_queue(
            cfg.sonarr_url, cfg.sonarr_api_key, "includeUnknownSeriesItems"
        )
        logging.info(f"Sonarr queue : {len(sonarr_hashes)} hash(es) en attente.")
        all_hashes |= sonarr_hashes
    else:
        logging.debug("Queue guard Sonarr désactivé (url ou api_key absent).")

    if cfg.radarr_url and cfg.radarr_api_key:
        radarr_hashes = _fetch_arr_queue(
            cfg.radarr_url, cfg.radarr_api_key, "includeUnknownMovieItems"
        )
        logging.info(f"Radarr queue : {len(radarr_hashes)} hash(es) en attente.")
        all_hashes |= radarr_hashes
    else:
        logging.debug("Queue guard Radarr désactivé (url ou api_key absent).")

    return all_hashes


# ==============================================================================
# Section WHITELIST
# ==============================================================================

@dataclass
class WhitelistEntry:
    """Entrée persistante pour un MIXED validé manuellement."""
    path:                      str
    category:                  str
    first_seen:                str   # ISO 8601 UTC
    last_seen:                 str   # ISO 8601 UTC
    orphan_count_at_whitelist: int
    last_orphan_count:         int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_whitelist(whitelist_path: str) -> dict[str, WhitelistEntry]:
    """
    Charge whitelist.json.
    Retourne un dict vide si le fichier est absent ou corrompu (+ warning).
    """
    if not os.path.isfile(whitelist_path):
        return {}
    try:
        with open(whitelist_path, "r", encoding="utf-8") as fh:
            raw: dict = json.load(fh)
        return {
            path: WhitelistEntry(**entry)
            for path, entry in raw.items()
        }
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        logging.warning(f"whitelist.json illisible ({e}). On repart avec une whitelist vide.")
        return {}


def save_whitelist(whitelist_path: str, entries: dict[str, WhitelistEntry]) -> None:
    """Sérialise la whitelist en JSON indenté."""
    data = {path: vars(entry) for path, entry in entries.items()}
    try:
        with open(whitelist_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except OSError as e:
        logging.error(f"Impossible d'écrire la whitelist : {e}")


def purge_missing_entries(
    whitelist: dict[str, WhitelistEntry]
) -> tuple[dict[str, WhitelistEntry], list[str]]:
    """
    Retire les entrées dont le path n'existe plus sur disque.
    Retourne (whitelist_purgée, liste_des_paths_retirés).
    """
    to_remove = [path for path in whitelist if not os.path.exists(path)]
    for path in to_remove:
        del whitelist[path]
    return whitelist, to_remove


def add_to_whitelist(
    whitelist: dict[str, WhitelistEntry],
    report: "EntryReport",
) -> dict[str, WhitelistEntry]:
    """Ajoute ou met à jour une entrée dans la whitelist."""
    now = _now_iso()
    existing = whitelist.get(report.path)
    whitelist[report.path] = WhitelistEntry(
        path                      = report.path,
        category                  = report.category,
        first_seen                = existing.first_seen if existing else now,
        last_seen                 = now,
        orphan_count_at_whitelist = existing.orphan_count_at_whitelist if existing else report.orphan_count,
        last_orphan_count         = report.orphan_count,
    )
    return whitelist


def refresh_whitelist_entry(
    whitelist: dict[str, WhitelistEntry],
    report: "EntryReport",
) -> dict[str, WhitelistEntry]:
    """Met à jour last_seen et last_orphan_count pour une entrée existante."""
    if report.path in whitelist:
        whitelist[report.path].last_seen         = _now_iso()
        whitelist[report.path].last_orphan_count = report.orphan_count
    return whitelist


def prompt_whitelist_entry(report: "EntryReport") -> bool:
    """
    Affiche les détails du MIXED et demande à l'utilisateur s'il faut le whitelister.
    Retourne True si l'utilisateur confirme.
    """
    kind = "dossier" if report.is_dir else "fichier"
    print()
    print(colorize(f"  Nouveau MIXED détecté ({kind}) :", "MIXED", "BOLD"))
    print(f"    {report.path}")
    print(colorize(
        f"    {report.orphan_count}/{report.total_media_count} fichier(s) orphelin(s) — {format_size(report.orphan_size_bytes)}",
        "DIM"
    ))
    for f in report.orphan_files:
        rel = os.path.relpath(f.path, report.path if report.is_dir else os.path.dirname(report.path))
        print(colorize(f"      - {rel}  [{format_size(f.size_bytes)}]", "DIM"))
    print()
    while True:
        answer = input("  Whitelister (ignorer aux prochains scans) ? [o/n] : ").strip().lower()
        if answer in ("o", "oui", "y", "yes"):
            return True
        if answer in ("n", "non", "no"):
            return False
        print("  Réponse invalide, tapez o ou n.")


# ==============================================================================
# Section SCANNER
# ==============================================================================

@dataclass
class FileInfo:
    """Informations d'un fichier media individuel."""
    path:       str
    nlink:      int   # nombre de hardlinks (st_nlink)
    size_bytes: int


@dataclass
class EntryReport:
    """Résultat d'analyse pour une entrée (fichier seul ou dossier torrent)."""
    path:              str
    category:          str
    status:            str
    is_dir:            bool          = False
    total_media_count: int           = 0
    total_size_bytes:  int           = 0
    orphan_files:      list[FileInfo] = field(default_factory=list)

    @property
    def orphan_count(self) -> int:
        return len(self.orphan_files)

    @property
    def orphan_size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.orphan_files)

    @property
    def is_fully_orphaned(self) -> bool:
        return self.total_media_count > 0 and self.orphan_count == self.total_media_count


def _stat_file(file_path: str) -> Optional[FileInfo]:
    """Lit les stats d'un fichier. Retourne None en cas d'erreur (+ warning)."""
    try:
        st = os.stat(file_path)
        return FileInfo(path=file_path, nlink=st.st_nlink, size_bytes=st.st_size)
    except OSError as e:
        logging.warning(f"Impossible de lire les stats de {file_path} : {e}")
        return None


def _collect_media_files(
    entry_path: str, media_extensions: frozenset[str]
) -> list[FileInfo]:
    """
    Collecte tous les fichiers media sous entry_path (fichier ou dossier).
    Les fichiers inaccessibles sont ignorés avec un warning.
    """
    if os.path.isfile(entry_path):
        ext = os.path.splitext(entry_path)[1].lower()
        if ext not in media_extensions:
            return []
        info = _stat_file(entry_path)
        return [info] if info else []

    if not os.path.isdir(entry_path):
        logging.warning(f"Entrée ignorée (ni fichier ni dossier) : {entry_path}")
        return []

    results = []
    for dirpath, dirnames, filenames in os.walk(entry_path):
        dirnames.sort()
        for fname in sorted(filenames):
            if os.path.splitext(fname)[1].lower() not in media_extensions:
                continue
            info = _stat_file(os.path.join(dirpath, fname))
            if info:
                results.append(info)
    return results


def _classify_media_files(media_files: list[FileInfo]) -> str:
    """Détermine le statut d'une entrée à partir de ses fichiers media."""
    if not media_files:
        return STATUS_EMPTY
    orphan_count = sum(1 for f in media_files if f.nlink == 1)
    if orphan_count == 0:
        return STATUS_OK
    if orphan_count == len(media_files):
        return STATUS_ORPHAN
    return STATUS_MIXED


def analyze_entry(
    entry_path: str, category: str, media_extensions: frozenset[str]
) -> EntryReport:
    """Analyse une entrée du filesystem et retourne son rapport de statut."""
    media_files = _collect_media_files(entry_path, media_extensions)
    status      = _classify_media_files(media_files)

    return EntryReport(
        path              = entry_path,
        category          = category,
        status            = status,
        is_dir            = os.path.isdir(entry_path),
        total_media_count = len(media_files),
        total_size_bytes  = sum(f.size_bytes for f in media_files),
        orphan_files      = [f for f in media_files if f.nlink == 1],
    )


def scan_category(
    category: str, base_path: str, media_extensions: frozenset[str]
) -> list[EntryReport]:
    """Scanne toutes les entrées directes d'un dossier de catégorie."""
    if not os.path.isdir(base_path):
        logging.warning(f"[{category.upper()}] Dossier introuvable : {base_path}")
        return []
    try:
        entry_names = sorted(os.listdir(base_path))
    except PermissionError as e:
        logging.error(f"[{category.upper()}] Permission refusée sur {base_path} : {e}")
        return []

    return [
        analyze_entry(os.path.join(base_path, name), category, media_extensions)
        for name in entry_names
    ]


# ==============================================================================
# Section ENRICHISSEMENT (queue guard + whitelist)
# ==============================================================================

def _apply_queue_guard(
    reports: list[EntryReport], queued_hashes: set[str]
) -> list[EntryReport]:
    """
    Marque QUEUED les entrées dont le nom de dossier/fichier correspond
    à un hash en queue arr.
    Note : on compare le basename de l'entrée aux hashes connus.
    Les entrées QUEUED ne sont jamais supprimées.
    """
    if not queued_hashes:
        return reports

    updated = []
    for report in reports:
        entry_name = os.path.basename(report.path.rstrip(os.sep)).lower()
        if entry_name in queued_hashes:
            report.status = STATUS_QUEUED
        updated.append(report)
    return updated


def _apply_whitelist(
    reports: list[EntryReport],
    whitelist: dict[str, WhitelistEntry],
) -> tuple[list[EntryReport], list[EntryReport]]:
    """
    Pour chaque rapport MIXED :
      - si déjà en whitelist → statut MIXED-WHITELISTED
    Pour chaque rapport ORPHAN qui était en whitelist MIXED :
      - garde ORPHAN (sera supprimé) et le signale pour retrait de whitelist
    Retourne (reports_mis_à_jour, orphans_promus_depuis_whitelist).
    """
    promoted_orphans = []
    updated = []

    for report in reports:
        if report.status == STATUS_MIXED and report.path in whitelist:
            report.status = STATUS_MIXED_WL
        elif report.status == STATUS_ORPHAN and report.path in whitelist:
            # Transition : était MIXED whitelisté, maintenant tout est orphelin
            promoted_orphans.append(report)
        updated.append(report)

    return updated, promoted_orphans


def enrich_reports(
    all_reports: dict[str, list[EntryReport]],
    queued_hashes: set[str],
    whitelist: dict[str, WhitelistEntry],
) -> tuple[dict[str, list[EntryReport]], list[EntryReport]]:
    """
    Applique queue guard et whitelist sur tous les rapports.
    Retourne (all_reports_enrichis, orphans_promus_depuis_whitelist).
    """
    all_promoted_orphans: list[EntryReport] = []

    for category in all_reports:
        reports = _apply_queue_guard(all_reports[category], queued_hashes)
        reports, promoted = _apply_whitelist(reports, whitelist)
        all_reports[category] = reports
        all_promoted_orphans.extend(promoted)

    return all_reports, all_promoted_orphans


# ==============================================================================
# Section STATISTIQUES
# ==============================================================================

@dataclass
class ScanStats:
    """Compteurs agrégés pour le résumé final."""
    orphan_count:            int = 0
    orphan_bytes:            int = 0
    mixed_new_count:         int = 0
    mixed_whitelisted_count: int = 0
    queued_count:            int = 0
    ok_count:                int = 0
    empty_count:             int = 0


def compute_stats(all_reports: dict[str, list[EntryReport]]) -> ScanStats:
    stats = ScanStats()
    for reports in all_reports.values():
        for r in reports:
            if r.status == STATUS_ORPHAN:
                stats.orphan_count += 1
                stats.orphan_bytes += r.total_size_bytes
            elif r.status == STATUS_MIXED:
                stats.mixed_new_count += 1
            elif r.status == STATUS_MIXED_WL:
                stats.mixed_whitelisted_count += 1
            elif r.status == STATUS_QUEUED:
                stats.queued_count += 1
            elif r.status == STATUS_OK:
                stats.ok_count += 1
            elif r.status == STATUS_EMPTY:
                stats.empty_count += 1
    return stats


def collect_orphan_entries(all_reports: dict[str, list[EntryReport]]) -> list[EntryReport]:
    return [r for reports in all_reports.values() for r in reports if r.status == STATUS_ORPHAN]


def collect_new_mixed_entries(all_reports: dict[str, list[EntryReport]]) -> list[EntryReport]:
    return [r for reports in all_reports.values() for r in reports if r.status == STATUS_MIXED]


def collect_whitelisted_entries(all_reports: dict[str, list[EntryReport]]) -> list[EntryReport]:
    return [r for reports in all_reports.values() for r in reports if r.status == STATUS_MIXED_WL]


def collect_queued_entries(all_reports: dict[str, list[EntryReport]]) -> list[EntryReport]:
    return [r for reports in all_reports.values() for r in reports if r.status == STATUS_QUEUED]


# ==============================================================================
# Section RENDERER
# ==============================================================================

# Codes ANSI
_ANSI = {
    "red":    "\033[91m",
    "yellow": "\033[93m",
    "green":  "\033[92m",
    "blue":   "\033[94m",
    "grey":   "\033[90m",
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
}

_STATUS_COLOR = {
    STATUS_ORPHAN:   "red",
    STATUS_MIXED:    "yellow",
    STATUS_MIXED_WL: "yellow",
    STATUS_QUEUED:   "blue",
    STATUS_OK:       "green",
    STATUS_EMPTY:    "grey",
}

SEP_THICK = "=" * 70
SEP_THIN  = "-" * 70


def colorize(text: str, *color_keys: str) -> str:
    """Applique une ou plusieurs couleurs/styles ANSI à un texte."""
    prefix = "".join(_ANSI.get(k, "") for k in color_keys)
    return f"{prefix}{text}{_ANSI['reset']}"


def format_size(size_bytes: int) -> str:
    """Formate une taille en bytes en chaîne lisible (KB/MB/GB)."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def print_header() -> None:
    print()
    print(colorize(SEP_THICK, "bold"))
    print(colorize("  HARDLINK CHECKER — RAPPORT", "bold", "blue"))
    print(colorize(SEP_THICK, "bold"))


def _print_orphan_files_detail(report: EntryReport) -> None:
    """Affiche les fichiers orphelins d'une entrée MIXED en sous-lignes."""
    base = report.path if report.is_dir else os.path.dirname(report.path)
    for f in report.orphan_files:
        rel_path = os.path.relpath(f.path, base)
        print(colorize(
            f"             {rel_path}  [{format_size(f.size_bytes)}]  nlink={f.nlink}",
            "dim"
        ))


def _print_entry_line(report: EntryReport) -> None:
    """Affiche une ligne de rapport pour une entrée."""
    color  = _STATUS_COLOR.get(report.status, "grey")
    label  = f"  {report.status:<18}"

    if report.status == STATUS_ORPHAN:
        file_count = f"({report.total_media_count} fichier(s))" if report.is_dir else ""
        size_str   = colorize(f"[{format_size(report.total_size_bytes)}]", color)
        print(f"{colorize(label, color, 'bold')}  {report.path}  {size_str}  {colorize(file_count, 'dim')}")

    elif report.status in (STATUS_MIXED, STATUS_MIXED_WL):
        ratio = colorize(
            f"[{report.orphan_count}/{report.total_media_count} orphelin(s) — {format_size(report.orphan_size_bytes)}]",
            color
        )
        print(f"{colorize(label, color, 'bold')}  {report.path}  {ratio}")
        if report.status == STATUS_MIXED:
            _print_orphan_files_detail(report)

    elif report.status == STATUS_QUEUED:
        print(f"{colorize(label, color, 'bold')}  {report.path}")

    elif report.status == STATUS_OK:
        print(f"{colorize(label, color, 'bold')}  {report.path}")

    elif report.status == STATUS_EMPTY:
        print(f"{colorize(label, 'grey', 'dim')}  {report.path}")


def print_category_section(
    category: str,
    base_path: str,
    reports: list[EntryReport],
    show_ok: bool,
) -> None:
    print()
    print(colorize(f"[ {category.upper()} ]  {base_path}", "bold"))
    print(colorize(SEP_THIN, "dim"))

    if not reports:
        print(colorize("  (dossier vide ou inaccessible)", "dim"))
        return

    visible_statuses = {STATUS_ORPHAN, STATUS_MIXED, STATUS_QUEUED}
    if show_ok:
        visible_statuses |= {STATUS_OK, STATUS_EMPTY, STATUS_MIXED_WL}

    for report in reports:
        if report.status in visible_statuses:
            _print_entry_line(report)


def print_whitelisted_section(whitelisted: list[EntryReport]) -> None:
    """Affiche la section dédiée aux MIXED whitelistés."""
    if not whitelisted:
        return
    print()
    print(colorize(SEP_THIN, "dim"))
    print(colorize("  MIXTES WHITELISTES  (ignorés automatiquement — vérifier manuellement si nécessaire)", "yellow", "bold"))
    print(colorize(SEP_THIN, "dim"))
    for report in whitelisted:
        ratio = colorize(
            f"[{report.orphan_count}/{report.total_media_count} orphelin(s) — {format_size(report.orphan_size_bytes)}]",
            "yellow"
        )
        print(f"  {colorize('MIXED-WL', 'yellow', 'bold')}  {report.path}  {ratio}")


def print_queued_section(queued: list[EntryReport]) -> None:
    """Affiche la section dédiée aux entrées en queue arr."""
    if not queued:
        return
    print()
    print(colorize(SEP_THIN, "dim"))
    print(colorize("  EN QUEUE SONARR/RADARR  (import en attente — jamais supprimés)", "blue", "bold"))
    print(colorize(SEP_THIN, "dim"))
    for report in queued:
        print(f"  {colorize('QUEUED', 'blue', 'bold')}  {report.path}")


def print_summary(stats: ScanStats) -> None:
    print()
    print(colorize(SEP_THICK, "bold"))
    print(colorize("  RESUME", "bold", "blue"))
    print(colorize(SEP_THIN, "dim"))

    if stats.orphan_count:
        print(colorize(
            f"  Orphelins          : {stats.orphan_count} entree(s)  —  {format_size(stats.orphan_bytes)} recuperables",
            "red", "bold"
        ))
    else:
        print(colorize("  Orphelins          : aucun", "green"))

    if stats.mixed_new_count:
        print(colorize(
            f"  Mixtes (nouveaux)  : {stats.mixed_new_count} entree(s)  — a decider (whitelist ?)",
            "yellow"
        ))
    if stats.mixed_whitelisted_count:
        print(colorize(
            f"  Mixtes whitelistes : {stats.mixed_whitelisted_count} entree(s)",
            "yellow", "dim"
        ))
    if stats.queued_count:
        print(colorize(f"  En queue arr       : {stats.queued_count} entree(s)", "blue"))

    print(colorize(f"  Sains (OK)         : {stats.ok_count} entree(s)", "dim"))
    if stats.empty_count:
        print(colorize(f"  Sans media         : {stats.empty_count} entree(s)", "dim"))

    print(colorize(SEP_THICK, "bold"))
    print()


def print_full_report(
    all_reports: dict[str, list[EntryReport]],
    cfg: AppConfig,
    stats: ScanStats,
) -> None:
    """Point d'entrée principal du renderer : affiche le rapport complet."""
    print_header()

    for category, reports in all_reports.items():
        base_path = cfg.category_paths.get(category, "?")
        print_category_section(category, base_path, reports, cfg.show_ok)

    whitelisted = collect_whitelisted_entries(all_reports)
    print_whitelisted_section(whitelisted)

    queued = collect_queued_entries(all_reports)
    print_queued_section(queued)

    print_summary(stats)


# ==============================================================================
# Section DELETION
# ==============================================================================

@dataclass
class DeletionResult:
    deleted_count: int           = 0
    freed_bytes:   int           = 0
    failed_paths:  list[str]     = field(default_factory=list)


def _delete_entry(report: EntryReport) -> bool:
    """Supprime un fichier ou un dossier. Retourne True si succès."""
    try:
        if report.is_dir:
            shutil.rmtree(report.path)
        else:
            os.remove(report.path)
        print(colorize(f"  [SUPPRIME]  {report.path}", "green"))
        return True
    except Exception as e:
        print(colorize(f"  [ERREUR]    {report.path}  —  {e}", "red"))
        return False


def _run_global_deletion(
    orphan_entries: list[EntryReport], skip_confirm: bool = False
) -> DeletionResult:
    """Supprime tous les orphelins en une seule confirmation globale."""
    result = DeletionResult()

    if not skip_confirm:
        print(colorize(f"\n  Suppression globale de {len(orphan_entries)} entree(s).", "yellow"))
        confirm = input("  Confirmer ? [oui/non] : ").strip().lower()
        if confirm not in ("oui", "o", "yes", "y"):
            print(colorize("  Suppression annulée.", "dim"))
            return result
        print()

    for entry in orphan_entries:
        if _delete_entry(entry):
            result.deleted_count += 1
            result.freed_bytes   += entry.total_size_bytes
        else:
            result.failed_paths.append(entry.path)
    return result


def _run_interactive_deletion(orphan_entries: list[EntryReport]) -> DeletionResult:
    """Demande confirmation pour chaque entrée individuellement."""
    result = DeletionResult()

    for entry in orphan_entries:
        kind     = "dossier" if entry.is_dir else "fichier"
        size_str = format_size(entry.total_size_bytes)
        print(f"\n  {kind}  {entry.path}  [{size_str}]")
        confirm = input("  Supprimer ? [o/n] : ").strip().lower()
        if confirm in ("o", "oui", "y", "yes"):
            if _delete_entry(entry):
                result.deleted_count += 1
                result.freed_bytes   += entry.total_size_bytes
            else:
                result.failed_paths.append(entry.path)
        else:
            print(colorize(f"  [IGNORE]    {entry.path}", "dim"))
    return result


def _ask_deletion_mode() -> str:
    """Demande à l'utilisateur le mode de suppression. Retourne 'i', 'g' ou 'a'."""
    print("  Choisissez le mode de suppression :")
    print("    [i]  Interactif — confirmer entrée par entrée")
    print("    [g]  Global     — supprimer tout d'un coup")
    print("    [a]  Annuler")
    print()
    while True:
        choice = input("  Votre choix [i/g/a] : ").strip().lower()
        if choice in ("i", "g", "a"):
            return choice
        print("  Réponse invalide, tapez i, g ou a.")


def run_deletion(orphan_entries: list[EntryReport], force: bool = False) -> DeletionResult:
    """
    Point d'entrée principal de la suppression.
    force=True → suppression globale sans confirmation.
    """
    if not orphan_entries:
        print(colorize("  Aucun orphelin à supprimer.", "dim"))
        return DeletionResult()

    print()
    print(colorize(SEP_THICK, "bold"))
    print(colorize("  SUPPRESSION DES ORPHELINS", "bold", "blue"))
    print(colorize(SEP_THICK, "bold"))
    print()

    if force:
        print(colorize(f"  Mode --force : {len(orphan_entries)} entree(s) supprimées sans confirmation.", "yellow"))
        print()
        result = _run_global_deletion(orphan_entries, skip_confirm=True)
    else:
        print(f"  {len(orphan_entries)} entree(s) orpheline(s) trouvee(s).\n")
        mode = _ask_deletion_mode()
        print()
        if mode == "a":
            print(colorize("  Suppression annulée.", "dim"))
            return DeletionResult()
        elif mode == "g":
            result = _run_global_deletion(orphan_entries)
        else:
            result = _run_interactive_deletion(orphan_entries)

    print()
    print(colorize(
        f"  {result.deleted_count}/{len(orphan_entries)} entree(s) supprimee(s)"
        f"  —  {format_size(result.freed_bytes)} liberes",
        "bold"
    ))
    if result.failed_paths:
        print(colorize(f"  {len(result.failed_paths)} erreur(s) lors de la suppression.", "red"))

    return result


# ==============================================================================
# Section MAIN
# ==============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hardlink Checker — détecte les fichiers orphelins dans les dossiers de téléchargements.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python hardlink-checker.py                        rapport + prompts whitelist
  python hardlink-checker.py --delete               rapport + suppression interactive ou globale
  python hardlink-checker.py --delete --force       rapport + suppression sans confirmation
  python hardlink-checker.py --no-interactive       rapport seul, aucun prompt (adapté pour cron)
  python hardlink-checker.py --show-ok              afficher aussi les entrées saines
  python hardlink-checker.py --config /srv/cfg.ini  config personnalisée
        """,
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=f"Chemin vers le fichier de configuration (défaut : {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Activer la suppression des entrées orphelines après le rapport",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Avec --delete : supprimer tous les orphelins sans confirmation (implique --delete)",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        dest="no_interactive",
        help="Désactiver tous les prompts (cron). Les nouveaux MIXED ne sont pas whitelistés.",
    )
    parser.add_argument(
        "--show-ok",
        action="store_true",
        dest="show_ok",
        help="Afficher aussi les entrées saines (nlink >= 2)",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)-8s  %(message)s",
        stream=sys.stderr,
    )


def _handle_whitelist_prompts(
    new_mixed_entries: list[EntryReport],
    whitelist: dict[str, WhitelistEntry],
    no_interactive: bool,
) -> dict[str, WhitelistEntry]:
    """
    Pour chaque nouveau MIXED, propose à l'utilisateur de le whitelister.
    En mode no_interactive, saute tous les prompts.
    """
    if no_interactive or not new_mixed_entries:
        return whitelist

    print(colorize(SEP_THIN, "dim"))
    print(colorize(
        f"  {len(new_mixed_entries)} nouveau(x) MIXED detecte(s) — décision requise.",
        "yellow", "bold"
    ))

    for report in new_mixed_entries:
        if prompt_whitelist_entry(report):
            whitelist = add_to_whitelist(whitelist, report)
            print(colorize(f"  Whitelisté : {report.path}", "green"))
        else:
            print(colorize(f"  Non whitelisté : sera réévalué au prochain scan.", "dim"))

    return whitelist


def _refresh_whitelisted_entries(
    whitelisted_entries: list[EntryReport],
    whitelist: dict[str, WhitelistEntry],
) -> dict[str, WhitelistEntry]:
    """Met à jour last_seen et last_orphan_count pour les entrées whitelistées encore présentes."""
    for report in whitelisted_entries:
        whitelist = refresh_whitelist_entry(whitelist, report)
    return whitelist


def _remove_promoted_orphans_from_whitelist(
    promoted_orphans: list[EntryReport],
    whitelist: dict[str, WhitelistEntry],
) -> dict[str, WhitelistEntry]:
    """
    Retire de la whitelist les entrées qui sont passées de MIXED à ORPHAN.
    Ces entrées seront supprimées normalement.
    """
    for report in promoted_orphans:
        if report.path in whitelist:
            del whitelist[report.path]
            logging.info(
                f"Retiré de la whitelist (devenu ORPHAN) : {report.path}"
            )
    return whitelist


def _scan_and_enrich(
    cfg: AppConfig,
    queued_hashes: set[str],
) -> tuple[dict[str, list[EntryReport]], dict[str, WhitelistEntry], list[EntryReport]]:
    """
    Effectue le scan filesystem, charge/purge la whitelist,
    applique le queue guard et la whitelist sur les rapports.
    Retourne (all_reports, whitelist_mise_a_jour, orphans_promus_depuis_whitelist).
    """
    all_reports: dict[str, list[EntryReport]] = {}
    for category, base_path in cfg.category_paths.items():
        logging.info(f"Scan [{category.upper()}] : {base_path}")
        all_reports[category] = scan_category(category, base_path, cfg.media_extensions)

    whitelist = load_whitelist(cfg.whitelist_path)
    whitelist, purged_paths = purge_missing_entries(whitelist)
    for path in purged_paths:
        logging.info(f"Retiré de la whitelist (path disparu) : {path}")

    all_reports, promoted_orphans = enrich_reports(all_reports, queued_hashes, whitelist)
    whitelist = _remove_promoted_orphans_from_whitelist(promoted_orphans, whitelist)

    return all_reports, whitelist, promoted_orphans


def _apply_cli_overrides(cfg: AppConfig, args: argparse.Namespace) -> AppConfig:
    """Surcharge les options de config par les flags CLI si activés."""
    if args.no_interactive:
        cfg.no_interactive = True
    if args.show_ok:
        cfg.show_ok = True
    return cfg


def main() -> None:
    args = parse_args()
    if args.force:
        args.delete = True

    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    cfg = _apply_cli_overrides(cfg, args)

    queued_hashes  = get_queued_hashes(cfg)
    all_reports, whitelist, promoted_orphans = _scan_and_enrich(cfg, queued_hashes)

    if promoted_orphans:
        print(colorize(
            f"\n  {len(promoted_orphans)} entree(s) whitelistee(s) sont maintenant totalement orphelines"
            " → suppression incluse dans le rapport.",
            "red", "bold"
        ))

    stats = compute_stats(all_reports)
    print_full_report(all_reports, cfg, stats)

    new_mixed   = collect_new_mixed_entries(all_reports)
    whitelisted = collect_whitelisted_entries(all_reports)
    whitelist   = _handle_whitelist_prompts(new_mixed, whitelist, cfg.no_interactive)
    whitelist   = _refresh_whitelisted_entries(whitelisted, whitelist)
    save_whitelist(cfg.whitelist_path, whitelist)

    orphan_entries = collect_orphan_entries(all_reports)
    if args.delete:
        run_deletion(orphan_entries, force=args.force)
    elif orphan_entries:
        print(colorize(
            f"  Relancez avec --delete pour supprimer les {len(orphan_entries)} orphelin(s).\n",
            "dim"
        ))

    sys.exit(1 if orphan_entries else 0)


if __name__ == "__main__":
    main()
