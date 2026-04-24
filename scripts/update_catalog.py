"""
Daily catalog update — runs via GitHub Actions.

Adds new Steam games (create-only, never overwrites enriched docs) and
refreshes SteamSpy genre/tag data. Requires write access via
STEAMSCOUT_ES_API_KEY env var (set as a GitHub Actions secret).
"""

import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from search.es_client import ESClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

STEAM_APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STEAMSPY_PAGE_URL = "https://steamspy.com/api.php?request=all&page={page}"


def main():
    es = ESClient()
    if not es.available:
        log.error("Cannot connect to Elasticsearch. Check STEAMSCOUT_ES_API_KEY secret.")
        sys.exit(1)

    log.info("Index currently has %d docs.", es.count())
    add_new_games(es)
    refresh_steamspy(es)
    log.info("Update complete. Index now has %d docs.", es.count())


def add_new_games(es):
    """Fetch the Steam app list and create docs for any games not yet in the index."""
    log.info("Fetching Steam app list...")
    try:
        r = requests.get(STEAM_APPLIST_URL, timeout=30)
        r.raise_for_status()
        apps = r.json()["applist"]["apps"]
        valid = [a for a in apps if a.get("name", "").strip()]
        log.info("Steam reports %d apps total.", len(valid))

        created = 0
        for i in range(0, len(valid), 500):
            batch = valid[i : i + 500]
            docs = [
                {
                    "app_id": a["appid"],
                    "name": a["name"],
                    "genres": [],
                    "tags": [],
                    "requirements_cached": False,
                }
                for a in batch
            ]
            created += es.bulk_create(docs)

        log.info("New games added to index: %d", created)
    except Exception as e:
        log.error("add_new_games failed: %s", e)


def refresh_steamspy(es):
    """Update genre/tag/developer fields from SteamSpy for all existing docs."""
    log.info("Fetching SteamSpy genre/tag data...")
    page = 0
    total = 0

    while True:
        try:
            r = requests.get(STEAMSPY_PAGE_URL.format(page=page), timeout=30)
            if not r.ok:
                break
            data = r.json()
            if not data:
                break

            docs = []
            for app_id_str, info in data.items():
                try:
                    app_id = int(app_id_str)
                except (ValueError, TypeError):
                    continue

                genre_str = info.get("genre") or ""
                genres = [g.strip() for g in genre_str.split(",") if g.strip()]
                tags_raw = info.get("tags") or {}
                tags = list(tags_raw.keys())[:20] if isinstance(tags_raw, dict) else []

                docs.append({
                    "app_id": app_id,
                    "genres": genres,
                    "tags": tags,
                    "developer": info.get("developer", ""),
                    "publisher": info.get("publisher", ""),
                })

            if docs:
                es.bulk_update_genres(docs)
                total += len(docs)

            log.info("SteamSpy page %d → %d apps (running total: %d)", page, len(docs), total)
            page += 1
            time.sleep(1.2)

            if len(data) < 100:
                break

        except Exception as e:
            log.error("SteamSpy page %d error: %s", page, e)
            break

    log.info("SteamSpy refresh done: %d apps updated.", total)


if __name__ == "__main__":
    main()
