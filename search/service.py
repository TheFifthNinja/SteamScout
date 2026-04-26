"""SearchService — the main interface for the search feature."""

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_SEARCH_CACHE_TTL  = 300  # seconds
_STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories?cc={cc}&l=en"
_SECTION_KEYS = {
    "sale":         "specials",
    "trending":     "top_sellers",
    "new_releases": "new_releases",
}


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
        self._search_cache: dict = {}  # (query, genres, tags, offset) → (results, timestamp)
        self._check_cache: dict = {}   # app_id → result dict

    def search(self, query: str, genres: list = None, tags: list = None, size: int = 30, offset: int = 0) -> dict:
        key = (query.strip().lower(), tuple(sorted(genres or [])), tuple(sorted(tags or [])), offset)
        cached = self._search_cache.get(key)
        if cached:
            results, ts = cached
            if time.time() - ts < _SEARCH_CACHE_TTL:
                return results
        results = self._es.search(query=query, genres=genres or [], tags=tags or [], size=size, offset=offset)
        self._search_cache[key] = (results, time.time())
        return results

    def check_game(self, app_id: int) -> Optional[dict]:
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
                "is_free": cached.get("is_free", False),
                "price_usd": cached.get("price_usd"),
            }
        else:
            reqs = self._fetch_requirements(app_id)
            if not reqs:
                return None
            if reqs.get("is_unlisted"):
                doc = cached or {"app_id": app_id, "genres": [], "tags": []}
                doc.update({
                    "app_id": app_id,
                    "app_type": "unavailable",
                    "requirements_cached": True,
                    "last_enriched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                self._es.upsert(doc)
                result = {
                    "app_id": app_id,
                    "name": doc.get("name", f"AppID {app_id}"),
                    "unlisted": True,
                }
                self._check_cache[app_id] = result
                return result
            # Persist to ES cache so next call is instant
            doc = cached or {"app_id": app_id, "genres": [], "tags": []}
            doc.update({
                "app_id": app_id,
                "name": reqs.get("name", doc.get("name", "")),
                "header_image": reqs.get("header_image", ""),
                "app_type": reqs.get("app_type", "game"),
                "min_reqs": reqs.get("minimum", {}),
                "rec_reqs": reqs.get("recommended", {}),
                "requirements_cached": True,
                "last_enriched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            self._es.upsert(doc)
            # Steam's requirements API doesn't return price — pull it from SteamSpy if we have it
            if cached:
                if reqs.get("price_usd") is None:
                    reqs["price_usd"] = cached.get("price_usd")
                if not reqs.get("is_free") and cached.get("is_free"):
                    reqs["is_free"] = True

        pc = self._pc_specs()
        if not pc:
            return None

        compat = self._check_compat(pc, reqs)

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
        return self._es.all_genres()

    def all_tags(self) -> list:
        return self._es.all_tags()

    def suggest(self, prefix: str) -> list:
        return self._es.suggest_names(prefix)

    def get_recommendations(
        self,
        checked_app_ids: list,
        sort: str = "popularity",
        min_rating: int = 0,
        limit: int = 30,
        price_filter: str = "all",
        offset: int = 0,
    ) -> list:
        is_free = True if price_filter == "free" else None

        if not checked_app_ids:
            return self._es.browse(sort=sort, min_rating=min_rating, size=limit, is_free=is_free, offset=offset)

        genre_counts: dict = {}
        for app_id in checked_app_ids[-30:]:
            doc = self._es.get(app_id)
            if doc:
                for genre in doc.get("genres", []):
                    genre_counts[genre] = genre_counts.get(genre, 0) + 1
        top_genres = sorted(genre_counts, key=genre_counts.__getitem__, reverse=True)[:5]

        results = self._es.get_similar(
            checked_app_ids[-15:],
            genres=top_genres or None,
            exclude_ids=checked_app_ids,
            size=limit,
            is_free=is_free,
            offset=offset,
        )
        if results:
            return results

        # MLT came up empty — fall back to genre browsing
        if top_genres:
            candidates = self._es.browse(
                genres=top_genres[:3],
                sort=sort,
                min_rating=min_rating,
                size=limit + len(checked_app_ids),
                is_free=is_free,
                offset=offset,
            )
            filtered = [g for g in candidates if g.get("app_id") not in checked_app_ids]
            if filtered:
                return filtered[:limit]

        return self._es.browse(sort=sort, min_rating=min_rating, size=limit, is_free=is_free, offset=offset)

    def get_catalogue(
        self,
        section: str = "sale",
        country: str = "us",
        sort: str = "popularity",
        min_rating: int = 0,
        limit: int = 30,
        price_filter: str = "all",
        offset: int = 0,
    ) -> list:
        # sale/trending/new_releases come from Steam directly; genre sections use ES
        if section in _SECTION_KEYS:
            return self._fetch_steam_featured(section, limit, country)
        is_free = True if price_filter == "free" else None
        return self._es.browse(genres=[section], sort=sort, min_rating=min_rating, size=limit, is_free=is_free, offset=offset)

    def _fetch_steam_featured(self, section: str, limit: int, country: str = "us") -> list:
        try:
            # "auto" → omit cc param so Steam uses the user's own IP for regional pricing
            if country == "auto":
                url = "https://store.steampowered.com/api/featuredcategories?l=en"
            else:
                url = _STEAM_FEATURED_URL.format(cc=country)
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data  = r.json()
            key   = _SECTION_KEYS[section]
            items = data.get(key, {}).get("items", [])[:limit]
            return [
                {
                    "app_id":           item.get("id"),
                    "name":             item.get("name", ""),
                    "header_image":     item.get("large_capsule_image") or item.get("header_image", ""),
                    "discount_percent": item.get("discount_percent", 0),
                    "original_price":   item.get("original_price") or 0,
                    "final_price":      item.get("final_price") or 0,
                    "is_free":          item.get("is_free_game", False) or item.get("final_price") == 0,
                    "genres":           [],
                    "compat_cache":     {},
                }
                for item in items
                if item.get("id")
            ]
        except Exception as e:
            log.error("Steam featured fetch failed for '%s': %s", section, e)
            return []

    @property
    def es_available(self) -> bool:
        return self._es.available
