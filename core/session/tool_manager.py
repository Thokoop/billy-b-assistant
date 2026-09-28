"""
Tool management for filtering and providing tools based on session mode.
"""

import os
from typing import Any

from ..base_tools import get_base_tools, get_user_tools
from ..config import (
    CAMERA_HARDWARE,
    OPENAI_API_KEY,
    REALTIME_AI_PROVIDER,
    WEB_SEARCH_ENABLED,
    is_conversation_state_enabled,
)
from ..logger import logger


class ToolManager:
    """Manages tool availability based on session mode."""

    def __init__(self):
        self._base_tools = None
        self._user_tools = None

    def get_tools(self, mode: str) -> list[dict[str, Any]]:
        """Get tools for given mode (guest/user)."""
        # Lazy load tools
        if self._base_tools is None:
            self._base_tools = get_base_tools()
        if self._user_tools is None:
            self._user_tools = get_user_tools()

        if mode == "guest":
            tools = list(self._base_tools)
        elif mode == "user":
            tools = list(self._base_tools) + list(self._user_tools)
        else:
            logger.warning(f"Unknown mode: {mode}, using base tools")
            tools = list(self._base_tools)

        if not is_conversation_state_enabled():
            tools = [t for t in tools if t.get("name") != "conversation_state"]

        camera_hardware = os.getenv("CAMERA_HARDWARE", CAMERA_HARDWARE).strip().lower()
        if camera_hardware not in {"rpi_camera", "usb_webcam"}:
            tools = [t for t in tools if t.get("name") != "describe_scene"]

        # Web search is implemented against the OpenAI API, so it is offered
        # only on an OpenAI build with a key. xAI has its own search tool; until
        # that is wired up, an xAI build simply does not get this one.
        web_search_enabled = os.getenv(
            "WEB_SEARCH_ENABLED", str(WEB_SEARCH_ENABLED)
        ).strip().lower() in {"true", "1", "yes", "on"}
        provider = (
            os.getenv("REALTIME_AI_PROVIDER", REALTIME_AI_PROVIDER or "openai")
            .strip()
            .lower()
        )
        if (
            not web_search_enabled
            or provider != "openai"
            or not os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
        ):
            tools = [t for t in tools if t.get("name") != "web_search"]

        # With no sources configured the digest can only ever fail, and a
        # failed call still costs a turn and an awkward answer. Take it away
        # so the model reaches for web search, or says it does not know.
        if not self._has_news_sources():
            tools = [t for t in tools if t.get("name") != "get_news_digest"]

        return tools

    @staticmethod
    def _has_news_sources() -> bool:
        try:
            from ..news_manager import load_news_sources

            return bool(load_news_sources())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not read news sources: {exc}", "\U0001f5de\ufe0f")
            # Unreadable is not the same as empty; keep the tool available.
            return True

    def is_available(self, name: str, mode: str = "user") -> bool:
        """Whether a tool is actually offered to the model right now."""
        return any(tool.get("name") == name for tool in self.get_tools(mode))

    def refresh_tools(self):
        """Refresh tool definitions (e.g., after song list changes)."""
        self._base_tools = get_base_tools()
        self._user_tools = get_user_tools()


# Singleton instance
tool_manager = ToolManager()
