from __future__ import annotations

import aiohttp

from core.config import HA_HOST, HA_LANG, HA_TOKEN


def ha_available():
    return bool(HA_HOST and HA_TOKEN)


async def send_conversation_prompt(prompt: str) -> str | None:
    if not ha_available():
        print("⚠️ Home Assistant not configured.")
        return None

    url = f"{HA_HOST.rstrip('/')}/api/conversation/process"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {"text": prompt, "language": HA_LANG}

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
