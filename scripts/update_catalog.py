"""
Daily catalog update — runs via GitHub Actions.

Two phases, both requiring write access via STEAMSCOUT_ES_API_KEY:

Phase 1 — refresh_app_list:
  Fetches the full Steam app list (~100 k entries) and bulk-creates any app IDs
  not yet in the index.  Existing docs are untouched (create-only).
  This is how brand-new Steam releases enter the index.

Phase 2 — refresh_steamspy:
  Fetches genre/tag/popularity/rating data from SteamSpy (paginated, ~1 req/s).
  Uses doc_as_upsert so games that just appeared in Phase 1 (or are new to
  SteamSpy) get their metadata filled in, and existing docs are updated.
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

STEAM_APPLIST_URL  = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STEAMSPY_PAGE_URL  = "https://steamspy.com/api.php?request=all&page={page}"


def main():
    es = ESClient()
    if not es.available:
        log.error("Cannot connect to Elasticsearch. Check STEAMSCOUT_ES_API_KEY secret.")
        sys.exit(1)

    log.info("Index currently has %d docs.", es.count())
    refresh_app_list(es)
    refresh_steamspy(es)
    log.info("Update complete. Index now has %d docs.", es.count())


def refresh_app_list(es):
    """
    Add any Steam apps not yet in the index.
    Uses bulk_create (create-only) so existing docs are never overwritten.
    """
    log.info("Fetching Steam app list...")
    try:
        r = requests.get(STEAM_APPLIST_URL, timeout=30)
        r.raise_for_status()
        apps  = r.json().get("applist", {}).get("apps", [])
        valid = [a for a in apps if a.get("name", "").strip()]
        log.info("Steam app list: %d named apps.", len(valid))

        docs = [
            {
                "app_id":              a["appid"],
                "name":                a["name"],
                "header_image":        f"https://cdn.akamai.steamstatic.com/steam/apps/{a['appid']}/header.jpg",
                "genres":              [],
                "tags":                [],
                "requirements_cached": False,
            }
            for a in valid
        ]

        added = es.bulk_create(docs)
        log.info("App list refresh done: %d new apps added to index.", added)
    except Exception as e:
        log.error("App list refresh failed: %s", e)


def refresh_steamspy(es):
    """
    Upsert genre/tag/developer/popularity/rating fields from SteamSpy.
    doc_as_upsert means new games (not yet in ES) are created, existing ones updated.
    """
    log.info("Fetching SteamSpy genre/tag data...")
    page  = 0
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
                genres    = [g.strip() for g in genre_str.split(",") if g.strip()]
                tags_raw  = info.get("tags") or {}
                tags      = list(tags_raw.keys())[:20] if isinstance(tags_raw, dict) else []

                owners_str = info.get("owners") or "0"
                try:
                    popularity = int(owners_str.split("..")[0].strip().replace(",", ""))
                except (ValueError, TypeError):
                    popularity = 0

                pos    = info.get("positive") or 0
                neg    = info.get("negative") or 0
                rating = round(pos / (pos + neg) * 100) if (pos + neg) > 0 else 0

                try:
                    price_cents = int(info.get("price") or 0)
                except (ValueError, TypeError):
                    price_cents = 0
                is_free   = price_cents == 0
                price_usd = price_cents / 100.0

                docs.append({
                    "app_id":       app_id,
                    "name":         info.get("name", ""),
                    "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg",
                    "genres":       genres,
                    "tags":         tags,
                    "developer":    info.get("developer", ""),
                    "publisher":    info.get("publisher", ""),
                    "popularity":   popularity,
                    "rating":       rating,
                    "is_free":      is_free,
                    "price_usd":    price_usd,
                    # Only set requirements_cached=False on new docs (doc_as_upsert
                    # won't overwrite this field on existing docs via the "doc" path,
                    # but we include it so freshly created docs enter the enrich queue).
                    "requirements_cached": False,
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

    log.info("SteamSpy refresh done: %d apps processed.", total)


if __name__ == "__main__":
    main()
