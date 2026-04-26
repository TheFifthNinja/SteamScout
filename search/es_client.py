"""
Elasticsearch client wrapper for SteamScout.

Defaults to the hosted Elastic Cloud instance. Override via env vars:
  STEAMSCOUT_ES_HOST      - custom endpoint (overrides cloud default)
  STEAMSCOUT_ES_API_KEY   - API key (overrides built-in key)
  STEAMSCOUT_ES_CLOUD_ID  - Elastic Cloud ID (alternative auth)
"""

import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

INDEX_NAME = "steam_games"

_CLOUD_ENDPOINT = "https://steamscout-fa1ff8.es.us-east-1.aws.elastic.cloud"
# Read-only key — safe to distribute. Override with STEAMSCOUT_ES_API_KEY for write access (indexing).
_CLOUD_API_KEY  = "XzFzWnZwMEJXYkVRSzExb1BMM0E6X29EdnZDOXlTSjZrbXBEbFhLUEFwZw=="

_MAPPING = {
    "mappings": {
        "properties": {
            "app_id":              {"type": "keyword"},
            "name":                {
                "type": "text",
                "analyzer": "standard",
                "fields": {"keyword": {"type": "keyword"}},
            },
            "genres":              {"type": "keyword"},
            "tags":                {
                "type": "keyword",
                "fields": {"text": {"type": "text", "analyzer": "standard"}},
            },
            "short_description":   {"type": "text"},
            "developer":           {
                "type": "keyword",
                "fields": {"text": {"type": "text", "analyzer": "standard"}},
            },
            "publisher":           {
                "type": "keyword",
                "fields": {"text": {"type": "text", "analyzer": "standard"}},
            },
            "app_type":            {"type": "keyword"},
            "is_free":             {"type": "boolean"},
            "price_usd":           {"type": "float"},
            "header_image":        {"type": "keyword", "index": False},
            "requirements_cached": {"type": "boolean"},
            "popularity":          {"type": "integer"},
            "rating":              {"type": "integer"},
            "min_reqs":            {"type": "object", "enabled": False},
            "rec_reqs":            {"type": "object", "enabled": False},
            "compat_cache":        {"type": "object", "enabled": False},
            "last_enriched":       {"type": "date"},
        }
    },
}


def _build_es():
    """Build an Elasticsearch client. Returns None if unavailable."""
    try:
        from elasticsearch import Elasticsearch
    except ImportError:
        log.warning("elasticsearch package not installed. Run: pip install elasticsearch>=8.0.0")
        return None

    cloud_id = os.environ.get("STEAMSCOUT_ES_CLOUD_ID")
    host     = os.environ.get("STEAMSCOUT_ES_HOST", _CLOUD_ENDPOINT)
    api_key  = os.environ.get("STEAMSCOUT_ES_API_KEY", _CLOUD_API_KEY)

    try:
        if cloud_id:
            es = Elasticsearch(cloud_id=cloud_id, api_key=api_key)
        else:
            es = Elasticsearch(host, api_key=api_key)

        # No cluster-level check here — connectivity is verified in _ensure_index
        log.info("Elasticsearch client created for %s", host)
        return es
    except Exception as e:
        log.warning("Failed to connect to Elasticsearch: %s", e)
        return None


def _is_auth_error(e: Exception) -> bool:
    """True for 403 / unauthorized responses — expected when using the read-only key."""
    s = str(e)
    return "403" in s or "unauthorized" in s.lower() or "AuthorizationException" in type(e).__name__


class ESClient:
    """
    Thin wrapper around the elasticsearch-py client.
    All public methods fail silently and return empty/None when ES is unavailable.
    Write methods are additionally skipped when the API key is read-only (403).
    """

    def __init__(self):
        self._es = _build_es()
        self._available = self._es is not None
        self._writable   = True   # flipped to False on first 403 write response
        if self._available:
            self._ensure_index()

    @property
    def available(self) -> bool:
        return self._available

    # ── Index management ───────────────────────────────────────────────────────

    def _ensure_index(self):
        try:
            if not self._es.indices.exists(index=INDEX_NAME):
                self._es.indices.create(
                    index=INDEX_NAME,
                    mappings=_MAPPING["mappings"],
                )
                log.info("Created Elasticsearch index '%s'", INDEX_NAME)
            else:
                self._es.indices.put_mapping(
                    index=INDEX_NAME,
                    properties=_MAPPING["mappings"]["properties"],
                )
        except Exception:
            # Read-only API key can't manage index metadata — verify read access instead
            try:
                self._es.search(index=INDEX_NAME, query={"match_all": {}}, size=1)
                log.info("Connected to Elasticsearch (read-only mode).")
            except Exception as e:
                log.error("Cannot read from Elasticsearch index: %s", e)
                self._available = False

    # ── Read ───────────────────────────────────────────────────────────────────

    def count(self) -> int:
        if not self._available:
            return 0
        try:
            return self._es.count(index=INDEX_NAME)["count"]
        except Exception:
            return 0

    def get(self, app_id: int) -> Optional[dict]:
        if not self._available:
            return None
        try:
            r = self._es.get(index=INDEX_NAME, id=str(app_id))
            return r["_source"]
        except Exception:
            return None

    def search(
        self,
        query: str,
        genres: list = None,
        tags: list = None,
        size: int = 30,
        offset: int = 0,
    ) -> list:
        if not self._available:
            return []
        try:
            filters = []
            if genres:
                filters.append({"terms": {"genres": genres}})
            if tags:
                filters.append({"terms": {"tags": tags}})

            must_not = [{"terms": {"app_type": [
                "dlc", "demo", "mod", "music", "soundtrack", "video", "advertising",
                "unavailable",
            ]}}]

            q = (query or "").strip()

            if q:
                if len(q) <= 2:
                    # Very short queries: prefix only — fuzzy on 1-2 chars returns garbage
                    base_query = {
                        "bool": {
                            "must": [{"match_phrase_prefix": {"name": q}}],
                            "filter":   filters,
                            "must_not": must_not,
                        }
                    }
                else:
                    base_query = {
                        "bool": {
                            "must": [{
                                "multi_match": {
                                    "query": q,
                                    # developer.text / tags.text use standard analyser → case-insensitive
                                    "fields": [
                                        "name^6",
                                        "tags.text^2",
                                        "developer.text^3",
                                        "publisher.text^1",
                                        "short_description^1",
                                    ],
                                    "type": "best_fields",
                                    "fuzziness": "AUTO",
                                    "prefix_length": 2,
                                    "operator": "or",
                                    "tie_breaker": 0.3,
                                }
                            }],
                            "should": [
                                # Exact phrase in name ranks highest (e.g. "grand theft auto")
                                {"match_phrase": {"name": {"query": q, "boost": 10}}},
                                # Prefix boost for autocomplete-style typing ("counter" → "Counter-Strike")
                                {"match_phrase_prefix": {"name": {"query": q, "boost": 7}}},
                                # Exact case-sensitive name (typed the full title)
                                {"term": {"name.keyword": {"value": q, "boost": 20}}},
                                # Tag phrase match (case-insensitive via .text sub-field)
                                {"match_phrase": {"tags.text": {"query": q, "boost": 3}}},
                                # Developer/publisher match — "valve" finds all Valve games
                                {"match": {"developer.text": {"query": q, "boost": 4}}},
                                {"match": {"publisher.text": {"query": q, "boost": 2}}},
                                # Soft boost: games with cached requirements → CHECK is instant
                                {"term": {"requirements_cached": True}},
                            ],
                            "filter":   filters,
                            "must_not": must_not,
                        }
                    }
            else:
                base_query = {"bool": {"must": [{"match_all": {}}], "filter": filters, "must_not": must_not}}

            resp = self._es.search(
                index=INDEX_NAME,
                query={
                    "function_score": {
                        "query": base_query,
                        "functions": [
                            # log(1 + owners) — popular games (CS2, Minecraft) outrank obscure ones
                            # factor 1.5: CS2 (100k owners) → +17 pts; unknown (10 owners) → +3.6 pts
                            {
                                "field_value_factor": {
                                    "field": "popularity",
                                    "modifier": "log1p",
                                    "factor": 1.5,
                                    "missing": 1,
                                }
                            },
                            # Positive review % (0-100) → up to +5 pts for 100% rating
                            {
                                "field_value_factor": {
                                    "field": "rating",
                                    "modifier": "none",
                                    "factor": 0.05,
                                    "missing": 50,
                                }
                            },
                        ],
                        "score_mode": "sum",
                        "boost_mode": "sum",
                    }
                },
                highlight={
                    "fields": {
                        "name":              {"number_of_fragments": 1, "fragment_size": 100},
                        "short_description": {"number_of_fragments": 1, "fragment_size": 150},
                    },
                    "pre_tags":  ["<mark>"],
                    "post_tags": ["</mark>"],
                },
                aggs={
                    "tags": {"terms": {"field": "tags", "size": 15}},
                },
                size=size,
                from_=offset,
                source=[
                    "app_id", "name", "genres", "tags", "short_description",
                    "header_image", "developer", "is_free", "app_type",
                    "requirements_cached", "compat_cache", "rating", "popularity",
                ],
            )
            results = []
            for h in resp["hits"]["hits"]:
                src = dict(h["_source"])
                hl = h.get("highlight", {})
                if hl:
                    src["_highlight"] = {
                        "name":    (hl.get("name")              or [None])[0],
                        "snippet": (hl.get("short_description") or [None])[0],
                    }
                results.append(src)
            facets = {
                k: [{"key": b["key"], "count": b["doc_count"]}
                    for b in v.get("buckets", [])]
                for k, v in resp.get("aggregations", {}).items()
            }
            return {
                "results": results,
                "facets":  facets,
                "total":   resp["hits"]["total"]["value"],
            }
        except Exception as e:
            log.error("ES search error: %s", e)
            return []

    def get_uncached(self, size: int = 10) -> list:
        """Return games that don't yet have requirements cached, for enrichment."""
        if not self._available:
            return []
        try:
            resp = self._es.search(
                index=INDEX_NAME,
                query={"term": {"requirements_cached": False}},
                size=size,
                source=["app_id", "name"],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            log.error("ES get_uncached error: %s", e)
            return []

    def browse(
        self,
        genres: list = None,
        tags: list = None,
        sort: str = "popularity",
        min_rating: int = 0,
        size: int = 30,
        is_free: bool = None,
        offset: int = 0,
    ) -> list:
        """
        Catalogue browsing with rich ES filtering and deterministic sorting.

        Genre filter uses three tiers of fallback so results appear even when the
        genres field is not yet populated from SteamSpy:
          1. Exact keyword on genres field (fast, precise)
          2. Genre name matched as text in the tags field (same SteamSpy source)
          3. Genre name matched in the game name (last resort)
        Docs missing the popularity/rating fields sort last automatically.
        """
        if not self._available:
            return []
        try:
            must_not = [
                {"terms": {"app_type": [
                    "dlc", "demo", "mod", "music", "soundtrack", "video", "advertising",
                    "unavailable",
                ]}},
            ]
            filters = []
            should = []

            if genres:
                should.append({"terms": {"genres": genres}})
                for g in genres:
                    should.append({"match": {"tags.text":  {"query": g, "boost": 0.5}}})
                    should.append({"match": {"name":       {"query": g, "boost": 0.2}}})

            if tags:
                should.append({"terms": {"tags": tags}})

            if min_rating > 0:
                filters.append({"range": {"rating": {"gte": min_rating}}})

            if is_free:
                filters.append({
                    "bool": {
                        "should": [
                            {"term": {"is_free":   True}},
                            {"term": {"price_usd": 0.0}},
                            # Fallback: SteamSpy genre tag until is_free/price_usd are indexed
                            {"term": {"genres": "Free to Play"}},
                        ],
                        "minimum_should_match": 1,
                    }
                })

            if should:
                base_q = {
                    "bool": {
                        "should": should,
                        "minimum_should_match": 1,
                        "filter":   filters,
                        "must_not": must_not,
                    }
                }
            elif filters:
                base_q = {"bool": {"filter": filters, "must_not": must_not}}
            else:
                base_q = {"bool": {"must_not": must_not}}

            # unmapped_type prevents a 400 if popularity/rating aren't in the
            # live mapping yet (e.g. put_mapping was blocked by a read-only key).
            _last = {"order": "desc", "missing": "_last", "unmapped_type": "integer"}
            sort_fields = {
                "popularity": [{"popularity": _last}, {"rating": _last}],
                "rating":     [{"rating": _last},     {"popularity": _last}],
                "name":       [{"name.keyword": "asc"}],
            }.get(sort, [{"popularity": _last}])

            resp = self._es.search(
                index=INDEX_NAME,
                query=base_q,
                sort=sort_fields,
                size=size,
                from_=offset,
                source=[
                    "app_id", "name", "genres", "tags", "header_image",
                    "developer", "app_type", "is_free", "price_usd", "popularity", "rating",
                    "requirements_cached", "compat_cache",
                ],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            log.error("ES browse error: %s", e)
            return []

    def get_similar(
        self,
        app_ids: list,
        genres: list = None,
        exclude_ids: list = None,
        size: int = 30,
        is_free: bool = None,
        offset: int = 0,
    ) -> list:
        """
        More Like This recommendation: find games similar to the given app_ids.
        Matches on tags, genres, and developer text fields.
        Optionally boosts results matching known genre preferences.
        """
        if not self._available or not app_ids:
            return []
        try:
            mlt = {
                "more_like_this": {
                    "fields": ["tags.text", "genres", "developer.text"],
                    "like": [
                        {"_index": INDEX_NAME, "_id": str(aid)}
                        for aid in app_ids[-15:]
                    ],
                    "min_term_freq":  1,
                    "max_query_terms": 25,
                    "min_doc_freq":   2,
                    "boost_terms":    1.0,
                }
            }

            must_not = [
                {"terms": {"app_type": [
                    "dlc", "demo", "mod", "music", "soundtrack", "video",
                    "unavailable",
                ]}},
            ]
            if exclude_ids:
                must_not.append({"ids": {"values": [str(a) for a in exclude_ids]}})

            filters = []
            if genres:
                filters.append({
                    "bool": {
                        "should": [
                            {"terms": {"genres": genres}},
                            *[
                                {"match": {"tags.text": {"query": g, "boost": 0.5}}}
                                for g in genres[:5]
                            ],
                        ],
                        "minimum_should_match": 1,
                    }
                })

            if is_free:
                filters.append({
                    "bool": {
                        "should": [
                            {"term": {"is_free":   True}},
                            {"term": {"price_usd": 0.0}},
                            {"term": {"genres": "Free to Play"}},
                        ],
                        "minimum_should_match": 1,
                    }
                })

            resp = self._es.search(
                index=INDEX_NAME,
                query={"bool": {"must": [mlt], "filter": filters, "must_not": must_not}},
                size=size,
                from_=offset,
                source=[
                    "app_id", "name", "genres", "tags", "header_image",
                    "developer", "app_type", "is_free", "price_usd", "popularity", "rating",
                    "requirements_cached", "compat_cache",
                ],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            log.error("ES get_similar error: %s", e)
            return []

    def all_genres(self) -> list:
        """Return all genre values present in the index, sorted alphabetically."""
        if not self._available:
            return []
        try:
            resp = self._es.search(
                index=INDEX_NAME,
                size=0,
                aggs={"genres": {"terms": {"field": "genres", "size": 150}}},
            )
            buckets = resp["aggregations"]["genres"]["buckets"]
            # "Free to Play" is a pricing category, not a genre — it's exposed via the price filter
            return sorted(b["key"] for b in buckets if b["key"] and b["key"] != "Free to Play")
        except Exception as e:
            log.error("ES all_genres error: %s", e)
            return []

    def all_tags(self) -> list:
        """Return the top 50 most common tags, ordered by popularity."""
        if not self._available:
            return []
        try:
            resp = self._es.search(
                index=INDEX_NAME,
                size=0,
                aggs={"tags": {"terms": {"field": "tags", "size": 50}}},
            )
            buckets = resp["aggregations"]["tags"]["buckets"]
            return [b["key"] for b in buckets if b["key"]]
        except Exception as e:
            log.error("ES all_tags error: %s", e)
            return []

    def suggest_names(self, prefix: str, size: int = 6) -> list:
        """Fast name-prefix lookup for search autocomplete. Returns app_id, name, header_image, genres."""
        if not self._available or not (prefix or "").strip():
            return []
        try:
            resp = self._es.search(
                index=INDEX_NAME,
                query={
                    "bool": {
                        "must": [{"match_phrase_prefix": {
                            "name": {"query": prefix.strip(), "max_expansions": 30}
                        }}],
                        "must_not": [{"terms": {"app_type": [
                            "dlc", "demo", "mod", "music", "soundtrack",
                            "video", "advertising", "unavailable",
                        ]}}],
                    }
                },
                sort=[
                    {"_score": "desc"},
                    {"popularity": {"order": "desc", "missing": "_last", "unmapped_type": "integer"}},
                ],
                size=size,
                source=["app_id", "name", "header_image", "genres"],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        except Exception as e:
            log.error("ES suggest_names error: %s", e)
            return []

    # ── Meta (cursor / config storage) ────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[dict]:
        """Retrieve a named metadata document (not a game doc)."""
        if not self._available:
            return None
        try:
            resp = self._es.get(index=INDEX_NAME, id=f"_meta_{key}")
            return resp["_source"]
        except Exception:
            return None

    def set_meta(self, key: str, value: dict):
        """Persist a named metadata document."""
        if not self._available or not self._writable:
            return
        try:
            self._es.index(index=INDEX_NAME, id=f"_meta_{key}", document=value)
        except Exception as e:
            if _is_auth_error(e):
                log.debug("ES set_meta blocked (read-only key).")
                self._writable = False
            else:
                log.error("ES set_meta error for key '%s': %s", key, e)

    # ── Write ──────────────────────────────────────────────────────────────────

    def upsert(self, doc: dict):
        if not self._available or not self._writable:
            return
        try:
            app_id = str(doc.get("app_id", ""))
            self._es.index(index=INDEX_NAME, id=app_id, document=doc)
        except Exception as e:
            if _is_auth_error(e):
                log.debug("ES write blocked (read-only key) — compat cache not persisted.")
                self._writable = False
            else:
                log.error("ES upsert error for app %s: %s", doc.get("app_id"), e)

    def bulk_upsert(self, docs: list) -> int:
        """Bulk-index a list of game dicts. Returns count of successful docs."""
        if not self._available or not docs:
            return 0
        try:
            from elasticsearch import helpers
            actions = [
                {
                    "_index": INDEX_NAME,
                    "_id": str(d["app_id"]),
                    **d,
                }
                for d in docs
                if d.get("app_id") is not None
            ]
            ok, _ = helpers.bulk(self._es, actions, raise_on_error=False)
            return ok
        except Exception as e:
            log.error("ES bulk_upsert error: %s", e)
            return 0

    def bulk_create(self, docs: list) -> int:
        """Create new game docs only — existing docs are untouched. Returns count of new docs created."""
        if not self._available or not docs:
            return 0
        try:
            from elasticsearch import helpers
            actions = [
                {
                    "_op_type": "create",
                    "_index": INDEX_NAME,
                    "_id": str(d["app_id"]),
                    **d,
                }
                for d in docs
                if d.get("app_id") is not None
            ]
            ok, _ = helpers.bulk(self._es, actions, raise_on_error=False)
            return ok
        except Exception as e:
            log.error("ES bulk_create error: %s", e)
            return 0

    def bulk_update_genres(self, docs: list) -> int:
        """
        Upsert genre/tag/developer/popularity/rating fields.

        On existing docs: updates only the supplied fields (requirements_cached
        is intentionally excluded so enrichment progress is never reset).
        On new docs: creates the full document including requirements_cached=False
        so the enrichment loop picks it up.
        """
        if not self._available or not docs:
            return 0
        try:
            from elasticsearch import helpers
            _metadata_fields = {
                "name", "header_image", "genres", "tags",
                "developer", "publisher", "popularity", "rating",
                "is_free", "price_usd",
            }
            actions = [
                {
                    "_op_type": "update",
                    "_index":   INDEX_NAME,
                    "_id":      str(d["app_id"]),
                    # Update only metadata fields on existing docs
                    "doc": {k: v for k, v in d.items()
                            if k != "app_id" and k in _metadata_fields},
                    # On create: include requirements_cached so doc enters enrich queue
                    "upsert": {k: v for k, v in d.items() if k != "app_id"},
                }
                for d in docs
                if d.get("app_id") is not None
            ]
            ok, _ = helpers.bulk(self._es, actions, raise_on_error=False)
            return ok
        except Exception as e:
            log.error("ES bulk_update_genres error: %s", e)
            return 0
