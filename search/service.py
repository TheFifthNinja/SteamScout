"""
SearchService — the main interface for the search feature.
Called directly from Overlay.py's pywebview Api class (thread-safe).
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


_SEARCH_CACHE_TTL = 300  # seconds — search results stay fresh for 5 minutes


class SearchService:
    def __init__(
        self,
        es_client,
        fetch_requirements_fn,
        check_compat_fn,
        pc_specs_fn,
    ):
        self._es = es_client
        self._fetch_requirements = fetch_requirements_fn
        self._check_compat = check_compat_fn
        self._pc_specs = pc_specs_fn
        # In-memory caches — live for the duration of the session
        self._search_cache: dict = {}   # (query, genres_tuple) → (results, timestamp)
        self._check_cache: dict = {}    # app_id → result dict

    # ── Public API (called from Overlay.py Api class) ─────────────────────────

    def search(self, query: str, genres: list = None, size: int = 20) -> list:
        """Full-text + genre search against ES. Returns list of game dicts."""
        key = (query.strip().lower(), tuple(sorted(genres or [])))
        cached = self._search_cache.get(key)
        if cached:
            results, ts = cached
            if time.time() - ts < _SEARCH_CACHE_TTL:
                return results
        results = self._es.search(query=query, genres=genres or [], size=size)
        self._search_cache[key] = (results, time.time())
        return results

    def check_game(self, app_id: int) -> Optional[dict]:
        """
        Return full compatibility data for a game.
        Uses in-memory cache first, then ES cache, then Steam API.
        """
        if app_id in self._check_cache:
            return self._check_cache[app_id]

        cached = self._es.get(app_id)

        if cached and cached.get("requirements_cached") and cached.get("min_reqs"):
            reqs = {
                "app_id": app_id,
                "name": cached.get("name", f"AppID {app_id}"),
                "header_image": cached.get("header_image", ""),
                "minimum": cached.get("min_reqs") or {},
                "recommended": cached.get("rec_reqs") or {},
            }
        else:
            reqs = self._fetch_requirements(app_id)
            if not reqs:
                return None
            # Persist to ES cache so next call is instant
            doc = cached or {"app_id": app_id, "genres": [], "tags": []}
            doc.update({
                "app_id": app_id,
                "name": reqs.get("name", doc.get("name", "")),
                "header_image": reqs.get("header_image", ""),
                "min_reqs": reqs.get("minimum", {}),
                "rec_reqs": reqs.get("recommended", {}),
                "requirements_cached": True,
                "last_enriched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self._es.upsert(doc)

        pc = self._pc_specs()
        if not pc:
            return None

        compat = self._check_compat(pc, reqs)

        # Update compat_cache in ES
        doc = self._es.get(app_id)
        if doc:
            doc["compat_cache"] = {
                "overall_min": compat.get("overall_min"),
                "overall_rec": compat.get("overall_rec"),
                "performance": compat.get("performance"),
            }
            self._es.upsert(doc)

        result = {
            "app_id": app_id,
            "name": reqs.get("name", f"AppID {app_id}"),
            "reqs": reqs,
            "compat": compat,
            "section": "Search",
        }
        self._check_cache[app_id] = result
        return result

    def all_genres(self) -> list:
        """Return all genres in the index (for the genre-filter chips)."""
        return self._es.all_genres()

    @property
    def es_available(self) -> bool:
        return self._es.available
