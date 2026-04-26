"""
Daily catalog update — runs via GitHub Actions.

Two modes, selected by env var:

  Default mode (no STEAMSPY_PAGE set):
    Phase 1 — refresh_app_list: bulk-create new Steam app IDs not yet in the index.
    Phase 2 — refresh_steamspy: advance the cursor by one page (fallback / local use).

  Matrix mode (STEAMSPY_PAGE=N):
    Fetch exactly page N from SteamSpy and write it to ES.
    Used by the GitHub Actions matrix job that runs all ~100 pages in parallel,
    each on a separate runner with its own IP address, bypassing SteamSpy's
    per-session rate limit.
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

# Steam API rotates endpoints occasionally; try each in order.
STEAM_APPLIST_URLS = [
    "https://api.steampowered.com/ISteamApps/GetAppList/v2/",
    "https://api.steampowered.com/ISteamApps/GetAppList/v0002/",
    "https://store.steampowered.com/api/applist/GetAppList/?include_games=1&include_dlc=0&include_software=0",
]
STEAMSPY_PAGE_URL = "https://steamspy.com/api.php?request=all&page={page}"
_STEAMSPY_RETRIES = 3
_STEAMSPY_BACKOFF = 5  # seconds; multiplied by attempt number


def main():
    es = ESClient()
    if not es.available:
        log.error("Cannot connect to Elasticsearch. Check STEAMSCOUT_ES_API_KEY secret.")
        sys.exit(1)

    page_env = os.environ.get("STEAMSPY_PAGE")
    if page_env is not None:
        # Matrix mode: one specific page, no app-list refresh
        try:
            page = int(page_env)
        except ValueError:
            log.error("STEAMSPY_PAGE must be an integer, got: %r", page_env)
            sys.exit(1)
        refresh_steamspy_page(es, page)
    else:
        # Default mode: app list + cursor-based single SteamSpy page
        log.info("Index currently has %d docs.", es.count())
        refresh_app_list(es)
        refresh_steamspy_cursor(es)
        log.info("Update complete. Index now has %d docs.", es.count())


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _fetch_steamspy_page(page: int) -> dict:
    """Fetch one SteamSpy page, retrying on empty/invalid JSON."""
    for attempt in range(_STEAMSPY_RETRIES):
        try:
            r = requests.get(STEAMSPY_PAGE_URL.format(page=page), timeout=30)
            if not r.ok:
                raise requests.HTTPError(response=r)
            text = r.text.strip()
            if not text:
                raise ValueError("empty response body")
            return r.json()
        except Exception as e:
            if attempt < _STEAMSPY_RETRIES - 1:
                wait = _STEAMSPY_BACKOFF * (attempt + 1)
                log.warning(
                    "SteamSpy page %d attempt %d failed (%s) — retrying in %ds",
                    page, attempt + 1, e, wait,
                )
                time.sleep(wait)
            else:
                raise


def _build_docs(data: dict) -> list:
    """Convert a raw SteamSpy page dict into ES upsert documents."""
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
            "app_id":              app_id,
            "name":                info.get("name", ""),
            "header_image":        f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg",
            "genres":              genres,
            "tags":                tags,
            "developer":           info.get("developer", ""),
            "publisher":           info.get("publisher", ""),
            "popularity":          popularity,
            "rating":              rating,
            "is_free":             is_free,
            "price_usd":           price_usd,
            "requirements_cached": False,
        })
    return docs


# ── Phase 1 ────────────────────────────────────────────────────────────────────

def refresh_app_list(es):
    """
    Add any Steam apps not yet in the index.
    Uses bulk_create (create-only) so existing docs are never overwritten.
    """
    log.info("Fetching Steam app list...")
    apps = None
    for url in STEAM_APPLIST_URLS:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            apps = r.json().get("applist", {}).get("apps", [])
            if apps:
                log.info("Steam app list fetched from %s", url)
                break
        except Exception as e:
            log.warning("Steam app list URL %s failed: %s", url, e)

    if not apps:
        log.error("All Steam app list URLs failed — skipping phase 1.")
        return

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


# ── Phase 2a — matrix mode (one page per runner) ───────────────────────────────

def refresh_steamspy_page(es, page: int):
    """
    Fetch exactly one SteamSpy page and write it to ES.
    Called by each GitHub Actions matrix job.
    """
    log.info("Matrix mode: fetching SteamSpy page %d...", page)
    try:
        data = _fetch_steamspy_page(page)
    except Exception as e:
        log.error("SteamSpy page %d failed: %s", page, e)
        sys.exit(1)

    if not data:
        log.info("SteamSpy page %d: no data (page beyond last).", page)
        return

    docs = _build_docs(data)
    if docs:
        es.bulk_update_genres(docs)
    log.info("SteamSpy page %d: %d apps updated.", page, len(docs))


# ── Phase 2b — cursor mode (one page per run, fallback / local) ────────────────

def refresh_steamspy_cursor(es):
    """
    Fetch one SteamSpy page per run using a persistent cursor stored in ES.
    Used as a fallback when the matrix workflow isn't available.
    """
    cursor     = es.get_meta("steamspy_cursor") or {}
    page       = cursor.get("next_page", 0)
    total_seen = cursor.get("total_seen", 0)
    log.info("Cursor mode: fetching SteamSpy page %d (%d apps seen so far)...", page, total_seen)

    try:
        data = _fetch_steamspy_page(page)
    except Exception as e:
        log.error("SteamSpy page %d failed after %d retries: %s — cursor unchanged.", page, _STEAMSPY_RETRIES, e)
        return

    if not data:
        log.info("SteamSpy page %d returned no data — resetting cursor to 0.", page)
        es.set_meta("steamspy_cursor", {"next_page": 0, "total_seen": total_seen})
        return

    docs = _build_docs(data)
    if docs:
        es.bulk_update_genres(docs)

    total_seen  += len(docs)
    is_last_page = len(data) < 100
    next_page    = 0 if is_last_page else page + 1
    es.set_meta("steamspy_cursor", {"next_page": next_page, "total_seen": total_seen})

    log.info(
        "SteamSpy page %d -> %d apps processed (cumulative: %d). Next run: page %d.",
        page, len(docs), total_seen, next_page,
    )
    if is_last_page:
        log.info("Reached last SteamSpy page — cursor reset to 0 for next cycle.")


if __name__ == "__main__":
    main()
