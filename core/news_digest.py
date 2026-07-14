"""News digest providers for headlines, weather forecasts, and sports results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from xml.etree import ElementTree

import requests

from .config import (
    NEWS_REQUEST_TIMEOUT_SECONDS,
)
from .logger import logger
from .news_manager import load_news_sources


OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

ESPN_SCOREBOARD_URLS = {
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "epl": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
}


@dataclass
class DigestResult:
    ok: bool
    category: str
    summary: str
    items: list[dict[str, Any]]
    source: str | None = None
    location: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "category": self.category,
            "summary": self.summary,
            "items": self.items,
        }
        if self.source:
            payload["source"] = self.source
        if self.location:
            payload["location"] = self.location
        if self.error:
            payload["error"] = self.error
        return payload


def get_news_digest(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch digest data and return a response suitable for function outputs."""
    category = str(args.get("category") or "headlines").strip().lower()
    max_items = _clamp_max_items(args.get("max_items"), default=3)

    if category == "headlines":
        return _get_headlines_digest(args, max_items).to_dict()
    if category == "weather":
        return _get_weather_digest(args).to_dict()
    if category == "sports":
        return _get_sports_digest(args, max_items).to_dict()

    return DigestResult(
        ok=False,
        category=category,
        summary=(
            "I can fetch headlines, weather, or sports. "
            "Please pick one of those categories."
        ),
        items=[],
        error=f"Unsupported category: {category}",
    ).to_dict()


def _clamp_max_items(raw_value: Any, default: int = 3) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, 5))


def _get_headlines_digest(args: dict[str, Any], max_items: int) -> DigestResult:
    query = str(args.get("query") or "").strip()
    subject = str(args.get("subject") or query).strip().lower()
    sources = load_news_sources()

    matching_sources = _select_matching_sources(sources, subject)
    if not matching_sources:
        if logger.get_level().name == "VERBOSE":
            logger.verbose(
                "get_news_digest: no configured matching sources, skipping headline fetch",
                "🗞️",
            )
        return DigestResult(
            ok=False,
            category="headlines",
            summary=(
                "I don't have any news sources configured yet. "
                "Please add at least one source in News Settings."
            ),
            items=[],
            error="No configured news sources",
        )

    items, source_names, errors = _collect_rss_items(
        matching_sources,
        max_items=max_items,
        query=query or None,
    )

    source_label_parts = list(source_names)
    source_name = ", ".join(source_label_parts) if source_label_parts else "RSS"
    title = (
        f"Top {max_items} headlines about {query}"
        if query
        else f"Top {max_items} headlines"
    )
    if errors and not items:
        return DigestResult(
            ok=False,
            category="headlines",
            summary="I couldn't fetch news headlines right now.",
            items=[],
            source=source_name,
            error="; ".join(errors[:2]),
        )

    if not items:
        return DigestResult(
            ok=False,
            category="headlines",
            summary="I couldn't find fresh headlines for that request.",
            items=[],
            source=source_name,
        )

    return DigestResult(
        ok=True,
        category="headlines",
        summary=title,
        items=items,
        source=source_name,
    )


def _get_weather_digest(args: dict[str, Any]) -> DigestResult:
    sources = load_news_sources()
    matching_sources = _select_matching_sources(sources, "weather")
    if not matching_sources:
        return _no_configured_sources_result("weather")

    for source in matching_sources:
        resolved_url = _resolve_source_fetch_url(source, "")
        if _is_open_meteo_forecast_url(resolved_url):
            source_name = str(source.get("name") or "Open-Meteo").strip()
            if logger.get_level().name == "VERBOSE":
                logger.verbose(
                    f"weather_digest: using open-meteo handler source={source_name} url={resolved_url}",
                    "🗞️",
                )
            return _get_weather_digest_open_meteo(
                args,
                source_name=source_name,
                source_url=resolved_url,
            )

    # No Open-Meteo source configured for weather category: use generic feed mode.
    return _get_generic_feed_digest(
        category="weather",
        sources=matching_sources,
        max_items=3,
        query=str(args.get("query") or args.get("location") or "weather").strip(),
        title="Latest weather headlines",
    )


def _select_matching_sources(
    sources: list[dict[str, Any]], subject: str = ""
) -> list[dict[str, Any]]:
    available = list(sources)
    if not available:
        return []

    if subject:
        subject_any = [
            source for source in available if _source_matches_subject(source, subject)
        ]
        if subject_any:
            return subject_any

    return available


def _no_configured_sources_result(category: str) -> DigestResult:
    return DigestResult(
        ok=False,
        category=category,
        summary=(
            f"I don't have any {category} sources configured yet. "
            "Please add at least one source in News Settings."
        ),
        items=[],
        error=f"No configured {category} sources",
    )


def _source_matches_subject(source: dict[str, Any], subject: str) -> bool:
    normalized_subject = subject.strip().lower()
    if not normalized_subject:
        return True
    topics = source.get("topics") or []
    if not topics:
        return False
    return any(
        normalized_subject in str(topic).strip().lower()
        or str(topic).strip().lower() in normalized_subject
        for topic in topics
    )


def _collect_rss_items(
    sources: list[dict[str, Any]], max_items: int, query: str | None = None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    collected: list[dict[str, Any]] = []
    source_names: list[str] = []
    errors: list[str] = []
    seen_titles: set[str] = set()
    per_source_items: list[list[dict[str, Any]]] = []
    fetch_limit = max(max_items * 4, 12) if query else max(max_items, 5)

    for source in sources:
        url = _resolve_source_fetch_url(source, query or "")
        if not url:
            continue
        name = str(source.get("name") or url).strip()
        try:
            response = requests.get(url, timeout=NEWS_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            items = _parse_feed_items(response.text, fetch_limit)
            normalized_items: list[dict[str, Any]] = []
            for item in items:
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                if query and not _item_matches_query(item, query):
                    continue
                item.setdefault("source", name)
                normalized_items.append(item)
            if normalized_items:
                source_names.append(name)
                per_source_items.append(normalized_items)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue

    # Blend sources in round-robin order so one very active feed does not
    # dominate the digest when multiple sources are configured.
    source_index = 0
    while len(collected) < max_items and per_source_items:
        current = per_source_items[source_index]
        if current:
            item = current.pop(0)
            title = (item.get("title") or "").strip()
            if title and title not in seen_titles:
                seen_titles.add(title)
                collected.append(item)
        if not current:
            per_source_items.pop(source_index)
            if not per_source_items:
                break
            source_index %= len(per_source_items)
        else:
            source_index = (source_index + 1) % len(per_source_items)

    return collected[:max_items], source_names, errors


def _get_generic_feed_digest(
    category: str,
    sources: list[dict[str, Any]],
    max_items: int,
    query: str = "",
    title: str = "Latest headlines",
) -> DigestResult:
    items, source_names, errors = _collect_rss_items(
        sources,
        max_items=max_items,
        query=query or None,
    )
    source_name = ", ".join(source_names) if source_names else "RSS"

    if errors and not items:
        return DigestResult(
            ok=False,
            category=category,
            summary=f"I couldn't fetch {category} updates right now.",
            items=[],
            source=source_name,
            error="; ".join(errors[:2]),
        )

    if not items:
        return DigestResult(
            ok=False,
            category=category,
            summary=f"I couldn't find fresh {category} updates for that request.",
            items=[],
            source=source_name,
        )

    return DigestResult(
        ok=True,
        category=category,
        summary=title,
        items=items,
        source=source_name,
    )


def _parse_feed_items(xml_text: str, max_items: int) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text)
    tag_name = _strip_xml_ns(root.tag).lower()
    if tag_name == "feed":
        return _parse_atom_items(root, max_items)
    return _parse_rss_items(root, max_items)


def _parse_rss_items(root: ElementTree.Element, max_items: int) -> list[dict[str, Any]]:
    channel = root.find("channel")
    if channel is None:
        return []

    parsed_items: list[dict[str, Any]] = []
    for item in channel.findall("item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        source = None
        source_el = item.find("source")
        if source_el is not None and source_el.text:
            source = source_el.text.strip()

        published_at = _format_pub_date(pub_date_raw)
        if title:
            parsed_items.append({
                "title": title,
                "link": link,
                "description": description,
                "source": source,
                "published_at": published_at,
            })

    return parsed_items


def _parse_atom_items(
    root: ElementTree.Element, max_items: int
) -> list[dict[str, Any]]:
    parsed_items: list[dict[str, Any]] = []
    feed_title = (_find_first_child_text(root, "title") or "").strip() or None
    entries = _find_children(root, "entry")

    for entry in entries[:max_items]:
        title = (_find_first_child_text(entry, "title") or "").strip()
        if not title:
            continue

        link = _find_atom_entry_link(entry)
        description = (_find_first_child_text(entry, "summary") or "").strip() or (
            _find_first_child_text(entry, "content") or ""
        ).strip()
        published_raw = (_find_first_child_text(entry, "updated") or "").strip() or (
            _find_first_child_text(entry, "published") or ""
        ).strip()
        parsed_items.append({
            "title": title,
            "link": link,
            "description": description,
            "source": feed_title,
            "published_at": _format_pub_date(published_raw),
        })

    return parsed_items


def _find_children(
    parent: ElementTree.Element, local_name: str
) -> list[ElementTree.Element]:
    return [child for child in list(parent) if _strip_xml_ns(child.tag) == local_name]


def _find_first_child_text(parent: ElementTree.Element, local_name: str) -> str:
    for child in list(parent):
        if _strip_xml_ns(child.tag) == local_name:
            return child.text or ""
    return ""


def _find_atom_entry_link(entry: ElementTree.Element) -> str:
    links = _find_children(entry, "link")
    if not links:
        return ""

    for link in links:
        rel = (link.attrib.get("rel") or "").strip().lower()
        href = (link.attrib.get("href") or "").strip()
        if href and (not rel or rel == "alternate"):
            return href

    return (links[0].attrib.get("href") or "").strip()


def _strip_xml_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _resolve_source_fetch_url(source: dict[str, Any], query: str) -> str:
    base_url = str(source.get("url") or "").strip()
    if not base_url:
        return ""

    resolved = base_url

    if "{{query}}" in resolved:
        normalized_query = query.strip()
        if not normalized_query:
            return ""
        resolved = resolved.replace("{{query}}", quote_plus(normalized_query))

    return resolved


def _format_pub_date(pub_date: str) -> str | None:
    if not pub_date:
        return None
    try:
        dt = parsedate_to_datetime(pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return pub_date


def _item_matches_query(item: dict[str, Any], query: str) -> bool:
    raw_query = query.strip().lower()
    if not raw_query:
        return True

    haystack = " ".join([
        str(item.get("title") or ""),
        str(item.get("description") or ""),
        str(item.get("source") or ""),
    ]).lower()
    if raw_query in haystack:
        return True

    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "there",
        "news",
        "about",
        "today",
        "latest",
        "what",
        "whats",
        "tell",
        "me",
    }
    tokens = [
        token
        for token in re.split(r"\W+", raw_query)
        if token and len(token) > 2 and token not in stop_words
    ]
    if not tokens:
        return False
    return any(token in haystack for token in tokens)


def _get_weather_digest_open_meteo(
    args: dict[str, Any],
    source_name: str = "Open-Meteo",
    source_url: str = "",
) -> DigestResult:
    location = str(args.get("location") or "").strip()
    source_coordinates = _extract_open_meteo_coordinates(source_url)
    source_timezone = _extract_open_meteo_timezone(source_url) or "auto"

    try:
        resolved_location = location
        latitude: float | None = None
        longitude: float | None = None

        if source_coordinates:
            latitude, longitude = source_coordinates
            resolved_location = (
                location if location else f"{latitude:.4f}, {longitude:.4f}"
            )
        else:
            if not location:
                return DigestResult(
                    ok=False,
                    category="weather",
                    summary=(
                        "I need a location for weather. You can say something like "
                        "'weather in Amsterdam'."
                    ),
                    items=[],
                    error="Missing location",
                )

            geocode_response = requests.get(
                OPEN_METEO_GEOCODE,
                params={
                    "name": location,
                    "count": 1,
                    "language": str(args.get("language") or "en").strip(),
                    "format": "json",
                },
                timeout=NEWS_REQUEST_TIMEOUT_SECONDS,
            )
            geocode_response.raise_for_status()
            geocode_data = geocode_response.json()
            results = geocode_data.get("results") or []
            if not results:
                return DigestResult(
                    ok=False,
                    category="weather",
                    summary=f"I couldn't find a location match for {location}.",
                    items=[],
                    source=source_name,
                )

            place = results[0]
            latitude = place.get("latitude")
            longitude = place.get("longitude")
            resolved_location = ", ".join(
                part
                for part in [
                    place.get("name"),
                    place.get("admin1"),
                    place.get("country"),
                ]
                if part
            )

        if latitude is None or longitude is None:
            return DigestResult(
                ok=False,
                category="weather",
                summary="I couldn't determine coordinates for weather lookup.",
                items=[],
                source=source_name,
                error="Missing coordinates",
            )

        forecast_response = requests.get(
            OPEN_METEO_FORECAST,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": ",".join([
                    "temperature_2m",
                    "apparent_temperature",
                    "wind_speed_10m",
                    "weather_code",
                ]),
                "daily": ",".join([
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                ]),
                "timezone": source_timezone,
                "forecast_days": 1,
            },
            timeout=NEWS_REQUEST_TIMEOUT_SECONDS,
        )
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()
    except Exception as exc:
        return DigestResult(
            ok=False,
            category="weather",
            summary="I couldn't fetch the weather forecast right now.",
            items=[],
            source=source_name,
            error=str(exc),
            location=location,
        )

    current = forecast_data.get("current") or {}
    daily = forecast_data.get("daily") or {}

    max_temp = _first_or_none(daily.get("temperature_2m_max"))
    min_temp = _first_or_none(daily.get("temperature_2m_min"))
    precip_prob = _first_or_none(daily.get("precipitation_probability_max"))
    weather_code = current.get("weather_code")

    items = [
        {
            "metric": "temperature_c",
            "value": current.get("temperature_2m"),
        },
        {
            "metric": "feels_like_c",
            "value": current.get("apparent_temperature"),
        },
        {
            "metric": "wind_kmh",
            "value": current.get("wind_speed_10m"),
        },
        {
            "metric": "today_high_c",
            "value": max_temp,
        },
        {
            "metric": "today_low_c",
            "value": min_temp,
        },
        {
            "metric": "precipitation_probability_percent",
            "value": precip_prob,
        },
        {
            "metric": "weather_code",
            "value": weather_code,
        },
    ]

    summary = (
        f"Weather for {resolved_location}: {current.get('temperature_2m')}°C now, "
        f"feels like {current.get('apparent_temperature')}°C. "
        f"Today ranges from {min_temp}°C to {max_temp}°C with up to "
        f"{precip_prob}% precipitation chance."
    )

    return DigestResult(
        ok=True,
        category="weather",
        summary=summary,
        items=items,
        source=source_name,
        location=resolved_location,
    )


def _first_or_none(values: Any) -> Any:
    if isinstance(values, list) and values:
        return values[0]
    return None


def _extract_open_meteo_coordinates(source_url: str) -> tuple[float, float] | None:
    if not source_url:
        return None
    try:
        parsed = urlparse(source_url)
        params = parse_qs(parsed.query, keep_blank_values=False)
    except Exception:
        return None

    lat = _safe_float(_first_query_param(params, "latitude"))
    lon = _safe_float(_first_query_param(params, "longitude"))
    if lat is None or lon is None:
        return None
    return lat, lon


def _extract_open_meteo_timezone(source_url: str) -> str | None:
    if not source_url:
        return None
    try:
        parsed = urlparse(source_url)
        params = parse_qs(parsed.query, keep_blank_values=False)
    except Exception:
        return None
    timezone = (_first_query_param(params, "timezone") or "").strip()
    return timezone or None


def _first_query_param(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    if not values:
        return ""
    return str(values[0])


def _safe_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _get_sports_digest(args: dict[str, Any], max_items: int) -> DigestResult:
    sources = load_news_sources()
    matching_sources = _select_matching_sources(sources, "sports")
    if not matching_sources:
        return _no_configured_sources_result("sports")

    requested_sport = str(args.get("sport") or "").strip().lower()
    team = str(args.get("team") or "").strip().lower()

    espn_candidates: list[tuple[dict[str, Any], str, str]] = []
    for source in matching_sources:
        resolved_url = _resolve_source_fetch_url(source, "")
        if _is_espn_scoreboard_url(resolved_url):
            inferred_sport = _infer_sport_from_espn_url(resolved_url)
            espn_candidates.append((source, resolved_url, inferred_sport))

    if espn_candidates:
        query = str(args.get("query") or "").strip().lower()

        def candidate_score(candidate: tuple[dict[str, Any], str, str]) -> int:
            source, url, inferred_sport = candidate
            searchable = " ".join([
                str(source.get("name") or ""),
                " ".join(source.get("topics") or []),
                url,
            ]).lower()
            score = 0
            if requested_sport and inferred_sport == requested_sport:
                score += 20
            if team and team in searchable:
                score += 50
            if query and query in searchable:
                score += 40
            return score

        chosen = max(espn_candidates, key=candidate_score)
        source, url, inferred_sport = chosen
        source_name = str(source.get("name") or "ESPN").strip()
        if logger.get_level().name == "VERBOSE":
            logger.verbose(
                "sports_digest: using espn handler "
                f"source={source_name} sport={inferred_sport} url={url}",
                "🗞️",
            )
        return _get_sports_digest_espn(
            args,
            max_items=max_items,
            url=url,
            source_name=source_name,
            sport_hint=inferred_sport or requested_sport or "nfl",
            team_override=team,
        )

    # No ESPN source configured for sports category: use generic feed mode.
    query_hint = str(
        args.get("query") or args.get("team") or args.get("sport") or "sports"
    ).strip()
    return _get_generic_feed_digest(
        category="sports",
        sources=matching_sources,
        max_items=max_items,
        query=query_hint,
        title="Latest sports headlines",
    )


def _get_sports_digest_espn(
    args: dict[str, Any],
    max_items: int,
    url: str | None = None,
    source_name: str = "ESPN",
    sport_hint: str = "",
    team_override: str = "",
) -> DigestResult:
    sport = str(sport_hint or args.get("sport") or "nfl").strip().lower()
    team = str(args.get("team") or "").strip().lower()
    if team_override:
        team = team_override
    if not url:
        url = ESPN_SCOREBOARD_URLS.get(sport)
    if not url:
        supported = ", ".join(sorted(ESPN_SCOREBOARD_URLS))
        return DigestResult(
            ok=False,
            category="sports",
            summary=f"I can check sports for: {supported}.",
            items=[],
            source=source_name,
            error=f"Unsupported sport: {sport}",
        )

    try:
        response = requests.get(url, timeout=NEWS_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return DigestResult(
            ok=False,
            category="sports",
            summary="I couldn't fetch sports results right now.",
            items=[],
            source=source_name,
            error=str(exc),
        )

    # Scoreboard endpoints expose `events`; team endpoints expose the team's
    # upcoming fixtures as `team.nextEvent`.
    events = data.get("events") or (data.get("team") or {}).get("nextEvent") or []
    parsed_events: list[dict[str, Any]] = []
    for event in events:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        if len(competitors) < 2:
            continue

        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            away, home = competitors[0], competitors[1]

        home_name = (home.get("team") or {}).get("displayName") or "Home"
        away_name = (away.get("team") or {}).get("displayName") or "Away"

        if team and team not in home_name.lower() and team not in away_name.lower():
            continue

        status = (
            (competition.get("status") or {})
            .get("type", {})
            .get("shortDetail", "Status unavailable")
        )
        parsed_events.append({
            "matchup": f"{away_name} at {home_name}",
            "score": f"{away.get('score', '-')} - {home.get('score', '-')}",
            "status": status,
            "start_time": event.get("date"),
        })
        if len(parsed_events) >= max_items:
            break

    if not parsed_events:
        team_note = f" for {args.get('team')}" if args.get("team") else ""
        return DigestResult(
            ok=False,
            category="sports",
            summary=f"I couldn't find current {sport.upper()} games{team_note}.",
            items=[],
            source=source_name,
        )

    generated_at = datetime.now(timezone.utc).isoformat()
    for event in parsed_events:
        event["generated_at"] = generated_at
    return DigestResult(
        ok=True,
        category="sports",
        summary=f"Latest {sport.upper()} scoreboard update",
        items=parsed_events,
        source=source_name,
    )


def _is_espn_scoreboard_url(url: str) -> bool:
    normalized = (url or "").strip().lower()
    return "site.api.espn.com/apis/site/v2/sports/" in normalized


def _infer_sport_from_espn_url(url: str) -> str:
    normalized = (url or "").strip().lower()
    if "/football/nfl/" in normalized:
        return "nfl"
    if "/basketball/nba/" in normalized:
        return "nba"
    if "/baseball/mlb/" in normalized:
        return "mlb"
    if "/hockey/nhl/" in normalized:
        return "nhl"
    if "/soccer/eng.1/" in normalized:
        return "epl"
    match = re.search(r"/sports/soccer/([^/?#]+)", normalized)
    if match:
        return match.group(1)
    return ""


def _is_open_meteo_forecast_url(url: str) -> bool:
    normalized = (url or "").strip().lower()
    return "api.open-meteo.com/v1/forecast" in normalized
