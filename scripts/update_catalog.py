# Daily catalog update — runs via GitHub Actions.
# Set STEAMSPY_PAGE=N for matrix mode (one page per runner, all ~100 run in parallel).
# Without it: refresh the Steam app list, then crawl SteamSpy pages up to a time budget.

import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from search.es_client import ESClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Steam rotates these sometimes, try in order until one works
STEAM_APPLIST_URLS = [
    "https://api.steampowered.com/ISteamApps/GetAppList/v2/",
    "https://api.steampowered.com/ISteamApps/GetAppList/v0002/",
    "https://store.steampowered.com/api/applist/GetAppList/?include_games=1&include_dlc=0&include_software=0",
]
STEAMSPY_PAGE_URL = "https://steamspy.com/api.php?request=all&page={page}"
RETRIES = 3
BACKOFF = 5       # base seconds — doubles each attempt: 5s, 10s, 20s
CURSOR_BUDGET = 4 * 60  # how long advance_cursor() is allowed to run (seconds)


def main():
    es = ESClient()
    if not es.available:
        log.error("Can't connect to Elasticsearch — is STEAMSCOUT_ES_API_KEY set?")
        sys.exit(1)

    page_str = os.environ.get("STEAMSPY_PAGE")
    if page_str is not None:
        try:
            page = int(page_str)
        except ValueError:
            log.error("STEAMSPY_PAGE must be an integer, got: %r", page_str)
            sys.exit(1)
        fetch_page(es, page)
    else:
        log.info("Starting update — index has %d docs.", es.count())
        refresh_app_list(es)
        advance_cursor(es)
        log.info("Done — index now has %d docs.", es.count())


def _get_steamspy_page(page, retry_empty=True):
    # retry_empty=False so matrix jobs on dead pages exit fast instead of waiting 60s
    for attempt in range(RETRIES):
        try:
            r = requests.get(STEAMSPY_PAGE_URL.format(page=page), timeout=30)
            if not r.ok:
                raise requests.HTTPError(response=r)
            body = r.text.strip()
            if not body:
                if not retry_empty:
                    return {}
                raise ValueError("empty response body")
            return r.json()
        except Exception as e:
            if attempt < RETRIES - 1:
                wait = BACKOFF * (2 ** attempt)  # 5s, 10s, 20s
                log.warning("Page %d attempt %d failed (%s) — retrying in %ds", page, attempt + 1, e, wait)
                time.sleep(wait)
            else:
                raise


def _build_docs(data):
    docs = []
    for app_id_str, info in data.items():
        try:
            app_id = int(app_id_str)
        except (ValueError, TypeError):
            continue

        name = info.get("name", "").strip()
        if not name:
            continue  # skip unnamed/placeholder entries

        genres = [g.strip() for g in (info.get("genre") or "").split(",") if g.strip()]
        tags_raw = info.get("tags") or {}
        tags = list(tags_raw.keys())[:20] if isinstance(tags_raw, dict) else []

        # owners comes back as "1,000,000 .. 2,000,000" — just take the lower bound
        owners = info.get("owners") or "0"
        try:
            popularity = int(owners.split("..")[0].strip().replace(",", ""))
        except (ValueError, TypeError):
            popularity = 0

        pos = info.get("positive") or 0
        neg = info.get("negative") or 0
        rating = round(pos / (pos + neg) * 100) if (pos + neg) > 0 else 0

        # price is integer cents — 999 means $9.99
        try:
            price_cents = int(info.get("price") or 0)
        except (ValueError, TypeError):
            price_cents = 0

        docs.append({
            "app_id": app_id,
            "name": name,
            "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg",
            "genres": genres,
            "tags": tags,
            "developer": info.get("developer", ""),
            "publisher": info.get("publisher", ""),
            "popularity": popularity,
            "rating": rating,
            "is_free": price_cents == 0,
            "price_usd": price_cents / 100.0,
            "requirements_cached": False,
        })
    return docs


def refresh_app_list(es):
    log.info("Fetching Steam app list...")
    apps = None
    for url in STEAM_APPLIST_URLS:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            apps = r.json().get("applist", {}).get("apps", [])
            if apps:
                log.info("Got app list from %s", url)
                break
        except Exception as e:
            log.warning("URL failed (%s): %s", url, e)

    if not apps:
        log.error("All Steam app list URLs failed — skipping.")
        return

    named = [a for a in apps if a.get("name", "").strip()]
    log.info("%d named apps in Steam app list.", len(named))

    docs = [
        {
            "app_id": a["appid"],
            "name": a["name"],
            "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{a['appid']}/header.jpg",
            "genres": [],
            "tags": [],
            "requirements_cached": False,
        }
        for a in named
    ]
    added = es.bulk_create(docs)
    log.info("Added %d new apps to the index.", added)


def fetch_page(es, page):
    # Page 1 often reuses the same runner as page 0 (same IP) — wait for the rate limit to reset.
    # For other pages, stagger by position within each group of 10 so they don't all fire at once.
    if page == 1:
        time.sleep(30)
    elif page % 10 != 0:
        time.sleep((page % 10) * 0.5)

    log.info("Fetching SteamSpy page %d...", page)
    try:
        data = _get_steamspy_page(page, retry_empty=False)
    except Exception as e:
        log.warning("Page %d failed: %s — assuming we're past the last page.", page, e)
        return

    if not data:
        log.info("Page %d came back empty — past the last SteamSpy page.", page)
        return

    docs = _build_docs(data)
    if docs:
        es.bulk_update_genres(docs)
    log.info("Page %d done — updated %d apps.", page, len(docs))


def advance_cursor(es):
    """
    Crawl SteamSpy pages starting from the saved cursor, continuing until
    CURSOR_BUDGET seconds have elapsed or we hit the last page.

    One page takes ~2s, so a 4-minute budget covers ~100 pages — the full
    catalog in one run. This is the fallback for when the matrix job isn't running.
    """
    cursor = es.get_meta("steamspy_cursor") or {}
    page = cursor.get("next_page", 0)
    total = cursor.get("total_seen", 0)
    deadline = time.time() + CURSOR_BUDGET

    log.info("Cursor at page %d (%d apps seen so far). Budget: %ds.", page, total, CURSOR_BUDGET)

    pages_done = 0
    while time.time() < deadline:
        log.info("Fetching page %d...", page)
        try:
            data = _get_steamspy_page(page)
        except Exception as e:
            log.error("Page %d failed after %d tries: %s — stopping here.", page, RETRIES, e)
            break

        if not data:
            log.info("Page %d was empty — wrapping cursor back to 0.", page)
            page = 0
            break

        docs = _build_docs(data)
        if docs:
            es.bulk_update_genres(docs)

        total += len(docs)
        pages_done += 1
        log.info("Page %d: %d apps (running total: %d).", page, len(docs), total)

        if len(data) < 100:
            log.info("Hit the last SteamSpy page — resetting for next cycle.")
            page = 0
            break

        page += 1

    es.set_meta("steamspy_cursor", {"next_page": page, "total_seen": total})
    log.info("Cursor saved at page %d. Processed %d pages this run.", page, pages_done)


if __name__ == "__main__":
    main()
