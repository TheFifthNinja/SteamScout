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
            "tags":                {"type": "keyword"},
            "short_description":   {"type": "text"},
            "developer":           {"type": "keyword"},
            "publisher":           {"type": "keyword"},
            "is_free":             {"type": "boolean"},
            "price_usd":           {"type": "float"},
            "header_image":        {"type": "keyword", "index": False},
            "requirements_cached": {"type": "boolean"},
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

        # ping() (HEAD /) is blocked on Serverless — use info() instead
        es.info()
        log.info("Connected to Elasticsearch at %s", host)
        return es
    except Exception as e:
        log.warning("Failed to connect to Elasticsearch: %s", e)
        return None


class ESClient:
    """
    Thin wrapper around the elasticsearch-py client.
    All public methods fail silently and return empty/None when ES is unavailable.
    """

    def __init__(self):
        self._es = _build_es()
        self._available = self._es is not None
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
        except Exception as e:
            log.error("Failed to create ES index: %s", e)
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
        size: int = 20,
    ) -> list:
        if not self._available:
            return []
        try:
            must = []
            if query and query.strip():
                must.append({
                    "multi_match": {
                        "query": query,
                        "fields": ["name^3", "short_description", "tags"],
                        "fuzziness": "AUTO",
                    }
                })
            else:
                must.append({"match_all": {}})

            filters = []
            if genres:
                filters.append({"terms": {"genres": genres}})

            resp = self._es.search(
                index=INDEX_NAME,
                query={"bool": {"must": must, "filter": filters}},
                size=size,
                source=[
                    "app_id", "name", "genres", "tags", "short_description",
                    "header_image", "developer", "is_free",
                    "requirements_cached", "compat_cache",
                ],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
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

    def all_genres(self) -> list:
        """Return all genre values present in the index, sorted."""
        if not self._available:
            return []
        try:
            resp = self._es.search(
                index=INDEX_NAME,
                size=0,
                aggs={"genres": {"terms": {"field": "genres", "size": 150}}},
            )
            buckets = resp["aggregations"]["genres"]["buckets"]
            return sorted(b["key"] for b in buckets if b["key"])
        except Exception as e:
            log.error("ES all_genres error: %s", e)
            return []

    # ── Write ──────────────────────────────────────────────────────────────────

    def upsert(self, doc: dict):
        if not self._available:
            return
        try:
            app_id = str(doc.get("app_id", ""))
            self._es.index(index=INDEX_NAME, id=app_id, document=doc)
        except Exception as e:
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
        """Partial-update only genre/tag/developer fields on existing docs."""
        if not self._available or not docs:
            return 0
        try:
            from elasticsearch import helpers
            actions = [
                {
                    "_op_type": "update",
                    "_index": INDEX_NAME,
                    "_id": str(d["app_id"]),
                    "doc": {k: v for k, v in d.items() if k != "app_id"},
                }
                for d in docs
                if d.get("app_id") is not None
            ]
            ok, _ = helpers.bulk(self._es, actions, raise_on_error=False)
            return ok
        except Exception as e:
            log.error("ES bulk_update_genres error: %s", e)
            return 0
