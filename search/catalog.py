"""
Catalog manager — fetches and indexes the Steam game catalog into Elasticsearch.

Phase 1 (runs once on startup if index is empty):
  1. Steam ISteamApps/GetAppList — ~100k app IDs + names in one call
  2. SteamSpy /api.php?request=all — genres, tags, developer info (paginated, ~1 req/s)

Phase 2 (continuous background enrichment):
  Slowly fetches full pc_requirements from the Steam Store API for uncached
  games (~1.5 s / game to stay under rate limits) and stores them so that
  future check_game() calls return instantly.
"""

import logging
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

STEAM_APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
STEAMSPY_PAGE_URL = "https://steamspy.com/api.php?request=all&page={page}"
_INDEX_BATCH = 500


class CatalogManager:
    def __init__(
        self,
        es_client,
        fetch_requirements_fn: Callable,
        check_compat_fn: Callable,
        pc_specs_fn: Callable,
    ):
        self._es = es_client
        self._fetch_requirements = fetch_requirements_fn
        self._check_compat = check_compat_fn
        self._pc_specs = pc_specs_fn

        self._total = 0
        self._indexed = 0
        self._enriched = 0
        self._phase = "idle"
        self._lock = threading.Lock()

    # ── Public ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            return {
                "es_available": self._es.available,
                "phase": self._phase,
                "total": self._total,
                "indexed": self._indexed,
                "enriched": self._enriched,
            }

    def start(self):
        """Start background indexing + enrichment threads."""
        if not self._es.available:
            log.warning("ES not available; catalog disabled.")
            return
        threading.Thread(
            target=self._run, daemon=True, name="CatalogManager"
        ).start()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self):
        try:
            current = self._es.count()
            if current >= 50_000:
                # Index pre-populated by the developer — users just read.
                # Skip both indexing and enrichment to avoid cloud write costs.
                log.info("Catalog: %d docs in ES; pre-populated, skipping all indexing.", current)
                with self._lock:
                    self._indexed = current
                    self._phase = "complete"
                return
            self._index_app_list()
            self._index_steamspy()
            self._enrich_loop()
        except Exception as e:
            log.error("CatalogManager._run crashed: %s", e, exc_info=True)

    # ── Phase 1a: Steam App List ───────────────────────────────────────────────

    def _index_app_list(self):
        with self._lock:
            self._phase = "fetching_applist"
        log.info("Catalog: fetching Steam app list...")
        try:
            r = requests.get(STEAM_APPLIST_URL, timeout=30)
            apps = r.json().get("applist", {}).get("apps", [])
            valid = [a for a in apps if a.get("name", "").strip()]

            with self._lock:
                self._total = len(valid)
                self._phase = "indexing_applist"

            log.info("Catalog: %d apps to index", len(valid))

            indexed = 0
            for i in range(0, len(valid), _INDEX_BATCH):
                batch = valid[i: i + _INDEX_BATCH]
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
                indexed += self._es.bulk_upsert(docs)
                with self._lock:
                    self._indexed = indexed

            log.info("Catalog: app list indexing done (%d docs)", indexed)
        except Exception as e:
            log.error("Catalog: app list fetch/index failed: %s", e)
        finally:
            with self._lock:
                self._phase = "applist_done"

    # ── Phase 1b: SteamSpy genres + tags ──────────────────────────────────────

    def _index_steamspy(self):
        with self._lock:
            self._phase = "fetching_steamspy"
        log.info("Catalog: fetching SteamSpy bulk data (genres/tags)...")

        page = 0
        total_enriched = 0
        while True:
            try:
                r = requests.get(
                    STEAMSPY_PAGE_URL.format(page=page), timeout=30
                )
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
                        "name": info.get("name", ""),
                        "genres": genres,
                        "tags": tags,
                        "developer": info.get("developer", ""),
                        "publisher": info.get("publisher", ""),
                        "requirements_cached": False,
                    })

                if docs:
                    self._es.bulk_upsert(docs)
                    total_enriched += len(docs)

                log.info("Catalog: SteamSpy page %d → %d apps", page, len(docs))
                page += 1

                # SteamSpy allows ~1 req/s
                time.sleep(1.2)

                if len(data) < 100:
                    break

            except Exception as e:
                log.error("Catalog: SteamSpy page %d error: %s", page, e)
                break

        log.info("Catalog: SteamSpy done (%d apps enriched)", total_enriched)
        with self._lock:
            self._phase = "steamspy_done"

    # ── Phase 2: Background enrichment (requirements) ─────────────────────────

    def _enrich_loop(self):
        with self._lock:
            self._phase = "enriching"
        log.info("Catalog: starting background enrichment loop.")

        while True:
            try:
                uncached = self._es.get_uncached(size=5)
                if not uncached:
                    time.sleep(60)
                    continue

                for game in uncached:
                    self._enrich_one(game)
                    time.sleep(1.5)

            except Exception as e:
                log.error("Catalog: enrichment loop error: %s", e)
                time.sleep(30)

    def _enrich_one(self, game: dict):
        app_id = int(game["app_id"])
        try:
            reqs = self._fetch_requirements(app_id)
            doc = self._es.get(app_id) or {"app_id": app_id, "name": game.get("name", ""), "genres": [], "tags": []}

            if reqs:
                pc = self._pc_specs()
                compat_cache = None
                if pc:
                    compat = self._check_compat(pc, reqs)
                    compat_cache = {
                        "overall_min": compat.get("overall_min"),
                        "overall_rec": compat.get("overall_rec"),
                        "performance": compat.get("performance"),
                    }
                doc.update({
                    "name": reqs.get("name", doc.get("name", "")),
                    "header_image": reqs.get("header_image", ""),
                    "min_reqs": reqs.get("minimum", {}),
                    "rec_reqs": reqs.get("recommended", {}),
                    "requirements_cached": True,
                    "last_enriched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                if compat_cache:
                    doc["compat_cache"] = compat_cache
            else:
                # Mark cached so we don't keep retrying games with no reqs
                doc["requirements_cached"] = True
                doc["last_enriched"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            self._es.upsert(doc)
            with self._lock:
                self._enriched += 1

        except Exception as e:
            log.error("Catalog: enrich_one failed for app %s: %s", app_id, e)
            # Still mark as cached to avoid infinite retries on broken entries
            try:
                doc = self._es.get(app_id) or {"app_id": app_id}
                doc["requirements_cached"] = True
                self._es.upsert(doc)
            except Exception:
                pass
