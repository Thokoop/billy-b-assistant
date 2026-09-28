from __future__ import annotations

import os
import time

import aiohttp

from core.config import HA_AGENT_ID, HA_HOST, HA_LANG, HA_TOKEN


def ha_available():
    return bool(HA_HOST and HA_TOKEN)


# Which assistant Home Assistant itself prefers. Only the websocket API knows,
# so the answer is cached: this runs in the middle of a spoken turn.
_PREFERRED_AGENT_CACHE_SECONDS = 600.0
_preferred_agent: str | None = None
_preferred_agent_checked_at = 0.0


def _websocket_url(host: str) -> str:
    base = host.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/api/websocket"
    return base + "/api/websocket"


async def preferred_agent_id(force: bool = False) -> str:
    """The conversation agent of Home Assistant's preferred Assist pipeline."""
    global _preferred_agent, _preferred_agent_checked_at
    if not ha_available():
        return ""
    fresh = (
        time.monotonic() - _preferred_agent_checked_at < _PREFERRED_AGENT_CACHE_SECONDS
    )
    if not force and _preferred_agent is not None and fresh:
        return _preferred_agent

    agent = ""
    try:
        # No connect-timeout argument here on purpose: its type changed across
        # aiohttp versions, and shipped Billys do not all run the same one.
        # Each read below is bounded instead.
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(_websocket_url(HA_HOST)) as ws,
        ):
            await ws.receive_json(timeout=5)  # auth_required
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            auth = await ws.receive_json(timeout=5)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"authentication failed: {auth.get('type')}")
            await ws.send_json({"id": 1, "type": "assist_pipeline/pipeline/list"})
            while True:
                message = await ws.receive_json(timeout=5)
                if message.get("id") == 1 and message.get("type") == "result":
                    break
            result = message.get("result") or {}
            preferred = result.get("preferred_pipeline")
            for pipeline in result.get("pipelines") or []:
                if pipeline.get("id") == preferred:
                    agent = str(pipeline.get("conversation_engine") or "").strip()
                    break
    except Exception as exc:
        print(f"⚠️ Could not read Home Assistant's preferred assistant: {exc}")
        agent = ""

    _preferred_agent = agent
    _preferred_agent_checked_at = time.monotonic()
    return agent


async def resolve_agent_id() -> str:
    """The agent to send with a request; empty means Home Assistant decides."""
    configured = os.getenv("HA_AGENT_ID", HA_AGENT_ID).strip()
    if configured and configured.lower() != "auto":
        return configured
    # Unset, or explicitly automatic: follow whatever Home Assistant prefers.
    return await preferred_agent_id()


async def send_conversation_prompt(prompt: str) -> str | None:
    if not ha_available():
        print("⚠️ Home Assistant not configured.")
        return None

    url = f"{HA_HOST.rstrip('/')}/api/conversation/process"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    payload: dict[str, str] = {"text": prompt, "language": HA_LANG}
    agent_id = await resolve_agent_id()
    if agent_id:
        payload["agent_id"] = agent_id

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, headers=headers, json=payload) as resp,
        ):
            if resp.status == 200:
                data = await resp.json()
                return data.get("response", "")
            print(f"⚠️ HA API returned HTTP {resp.status}")
            return None
    except Exception as e:
        print(f"❌ Error reaching Home Assistant API: {e}")
        return None
