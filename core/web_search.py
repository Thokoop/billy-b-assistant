"""Live web search for questions the model cannot answer from what it knows."""

from __future__ import annotations

import os
from typing import Any

from .config import (
    OPENAI_API_KEY,
    WEB_SEARCH_CONTEXT_SIZE,
    WEB_SEARCH_MODEL,
    WEB_SEARCH_TIMEOUT_SECONDS,
)
from .logger import logger


# Spoken answers, so no lists, no markdown and no URLs. The realtime model
# reads this result out nearly verbatim, so it has to arrive speakable.
_ANSWER_STYLE = (
    "You are answering a question that will be read out loud by a voice "
    "assistant. Reply with at most three short sentences of plain spoken "
    "language. No lists, no markdown, no links, no URLs. Name the source "
    "publication only when it matters for trust. Give the date of anything "
    "recent. If the search does not answer the question, say so plainly "
    "instead of guessing."
)


def _device_timezone() -> str:
    """IANA timezone of this device, for locally relevant results."""
    try:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            return link.split("/zoneinfo/", 1)[1]
    except Exception:
        pass
    return ""


def web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search the web and return a short spoken-style answer."""
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "ok": False,
            "query": "",
            "summary": "I need something to search for.",
            "error": "Missing query",
        }
    if not OPENAI_API_KEY:
        return {
            "ok": False,
            "query": query,
            "summary": "Searching the internet is not set up on this Billy.",
            "error": "Missing OpenAI API key",
        }

    tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": WEB_SEARCH_CONTEXT_SIZE,
    }
    timezone = _device_timezone()
    if timezone:
        tool["user_location"] = {"type": "approximate", "timezone": timezone}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=WEB_SEARCH_MODEL,
            tools=[tool],
            instructions=_ANSWER_STYLE,
            input=query,
            timeout=WEB_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"Web search failed: {exc}", "🌐")
        return {
            "ok": False,
            "query": query,
            "summary": "I couldn't reach the internet just now.",
            "error": str(exc),
        }

    summary = (getattr(response, "output_text", "") or "").strip()
    if not summary:
        return {
            "ok": False,
            "query": query,
            "summary": "The search came back empty.",
            "error": "Empty response",
        }

    return {
        "ok": True,
        "query": query,
        "summary": summary,
        "sources": _cited_titles(response),
    }


def _cited_titles(response: Any) -> list[str]:
    """Titles of the pages behind the answer.

    Titles only, never URLs: this goes to a model that speaks its input, and
    nobody wants a fish reading out an address bar.
    """
    titles: list[str] = []
    try:
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                for annotation in getattr(part, "annotations", None) or []:
                    title = (getattr(annotation, "title", "") or "").strip()
                    if title and title not in titles:
                        titles.append(title)
    except Exception:
        return titles[:3]
    return titles[:3]
