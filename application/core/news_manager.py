"""Persistent news source registry for headline feeds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ROOT_DIR


NEWS_SOURCES_PATH = Path(ROOT_DIR) / "news_sources.json"


def get_default_sources() -> list[dict[str, Any]]:
    return [
        {
            "id": "google-global-en",
            "name": "Google News (Global/en)",
            "url": "https://news.google.com/rss/search?q={{query}}&hl=en-US&gl=US&ceid=US:en",
            "topics": ["general"],
        },
        {
            "id": "billy-project-releases",
            "name": "Billy Project Releases",
            "url": "https://github.com/Thokoop/billy-b-assistant/releases.atom",
            "topics": ["billy", "project", "release", "update", "changelog"],
        },
    ]


def _normalize_sources(raw_sources: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_sources, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, source in enumerate(raw_sources):
        if not isinstance(source, dict):
            continue

        source_id = str(source.get("id") or f"source-{index}").strip()
        if not source_id or source_id in seen_ids:
            continue

        url = str(source.get("url") or "").strip()
        if not url:
            continue

        normalized_source = {
            "id": source_id,
            "name": str(source.get("name") or source_id).strip(),
            "url": url,
            "topics": _normalize_topics(source.get("topics")),
        }
        seen_ids.add(source_id)
        normalized.append(normalized_source)
    return normalized


def load_news_sources() -> list[dict[str, Any]]:
    if not NEWS_SOURCES_PATH.exists():
        defaults = get_default_sources()
        save_news_sources(defaults)
        return defaults

    try:
        payload = json.loads(NEWS_SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:
        defaults = get_default_sources()
        save_news_sources(defaults)
        return defaults

    raw_sources = payload.get("sources")
    sources = _normalize_sources(raw_sources)
    if raw_sources is None:
        sources = get_default_sources()
        save_news_sources(sources)
        return sources

    return sources


def save_news_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _normalize_sources(sources)

    payload = {"version": 1, "sources": normalized}
    NEWS_SOURCES_PATH.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return normalized


def _normalize_topics(raw_topics: Any) -> list[str]:
    if raw_topics is None:
        return []

    if isinstance(raw_topics, str):
        raw_list = [part.strip().lower() for part in raw_topics.split(",")]
    elif isinstance(raw_topics, list):
        raw_list = [str(part).strip().lower() for part in raw_topics]
    else:
        return []

    topics: list[str] = []
    seen: set[str] = set()
    for topic in raw_list:
        if not topic or topic in seen:
            continue
        seen.add(topic)
        topics.append(topic)
    return topics
