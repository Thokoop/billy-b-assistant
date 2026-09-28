"""
Base tools that work with any realtime conversation provider.
These are the custom Billy functions.
"""

from typing import Any

from .config import PERSONALITY
from .song_manager import song_manager


def _knowledge_topics_hint() -> str:
    """Name the trigger topics of the uploaded knowledge folders."""
    try:
        from .knowledge_manager import knowledge_manager

        topics: list[str] = []
        for folder in knowledge_manager.list_folders():
            for topic in folder.get("trigger_topics") or []:
                topic = str(topic).strip()
                if topic and topic not in topics:
                    topics.append(topic)
    except Exception:
        return ""
    if not topics:
        return ""
    return f"Uploaded topics: {', '.join(topics[:20])}. "


def _configured_topics_hint() -> str:
    """Name the topics this device actually has feeds for.

    Without this the model has to guess whether a subject is covered, and it
    guesses badly: a Bitcoin price went to a headlines feed that did not exist.
    """
    try:
        from .news_manager import load_news_sources

        topics: list[str] = []
        for source in load_news_sources():
            for topic in source.get("topics") or []:
                topic = str(topic).strip()
                if topic and topic not in topics:
                    topics.append(topic)
    except Exception:
        return ""
    if not topics:
        return ""
    return f"Configured topics: {', '.join(topics[:20])}. "


def get_base_tools() -> list[dict[str, Any]]:
    """Get the base tools that work with any provider"""
    return [
        {
            "name": "update_personality",
            "type": "function",
            "description": "Adjusts Billy's personality traits. Accepts numeric values (0-100) or level names (min/low/med/high/max). Call this function when users request personality changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    **{
                        trait: {
                            "oneOf": [
                                {"type": "integer", "minimum": 0, "maximum": 100},
                                {
                                    "type": "string",
                                    "enum": ["min", "low", "med", "high", "max"],
                                },
                            ]
                        }
                        for trait in vars(PERSONALITY)
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "play_song",
            "type": "function",
            "description": song_manager.get_dynamic_tool_description(),
            "parameters": {
                "type": "object",
                "properties": {"song": {"type": "string"}},
                "required": ["song"],
            },
        },
        {
            "name": "smart_home_command",
            "type": "function",
            "description": "Send a DIRECT command or question to Home Assistant, phrased the way you would say it to a voice assistant: short, plain, and using the name the device actually has (e.g., 'Turn on the kitchen lights', 'What is the energy meter'). Do not describe what you want in a long sentence and do not invent entity names. **CRITICAL: Only call this for DIRECT commands. If the user asks you to ASK/CHECK/CONFIRM first (e.g., 'ask if lights should be on'), do NOT call this function - just speak the question and wait for their answer.**",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The DIRECT command to send to Home Assistant (not a question)",
                    }
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "get_mood",
            "type": "function",
            "description": "Get Billy's current temporary mood state. Use when users ask how Billy feels, what mood he is in, or why his tone seems different.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "set_mood",
            "type": "function",
            "description": "Set Billy's temporary mood without changing persona or long-term personality. Use when users ask Billy to be neutral, calm, cheerful, warm, curious, focused, playful, mischievous, excited, surprised, sleepy, bored, sad, anxious, flustered, annoyed, grumpy, or dramatic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {
                        "type": "string",
                        "enum": [
                            "neutral",
                            "calm",
                            "cheerful",
                            "warm",
                            "curious",
                            "focused",
                            "playful",
                            "mischievous",
                            "excited",
                            "surprised",
                            "sleepy",
                            "bored",
                            "sad",
                            "anxious",
                            "flustered",
                            "annoyed",
                            "grumpy",
                            "dramatic",
                        ],
                        "description": "The temporary mood to apply.",
                    }
                },
                "required": ["mood"],
                "additionalProperties": False,
            },
        },
        {
            "name": "conversation_state",
            "type": "function",
            "description": "Silent end-of-turn signal. This is an internal API call, never speech: its name and arguments are never voiced, spelled out, read aloud or appended to your answer, and your spoken words end with your last real sentence. Required at the end of every turn: speak your answer first, then make this call. Set expects_follow_up=true if you asked a question or need user input, false for complete statements. Optionally set mood_event to one matching event if the user's message or the conversation meaningfully affected Billy's mood. Never make this call as your ONLY response - generate spoken audio first, then call this function. If audio is unclear, say 'I didn't catch that' before calling this. The system depends on this call arriving after every turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expects_follow_up": {"type": "boolean"},
                    "suggested_prompt": {"type": "string"},
                    "reason": {"type": "string"},
                    "mood_event": {
                        "type": "string",
                        "enum": [
                            "none",
                            "user_playful",
                            "user_kind",
                            "user_frustrated",
                            "conversation_fun",
                            "conversation_serious",
                            "billy_helpful",
                            "billy_confused",
                            "billy_bored",
                        ],
                        "description": "Optional mood event for this turn. Use none when there was no meaningful mood change.",
                    },
                    "mood_event_reason": {
                        "type": "string",
                        "description": "Short reason for the mood event, for logs/debugging.",
                    },
                },
                "required": ["expects_follow_up"],
            },
        },
        {
            "name": "identify_user",
            "type": "function",
            "description": "Call this ONLY when someone explicitly introduces themselves by stating their own name (e.g., 'I am Tom', 'My name is Sarah', 'Hey billy it is tom'). Do NOT call this when someone greets you by name (like 'Hello Billy' or 'Hey Billy'). Only call when they are telling you their own name to switch from guest mode to user mode. IMPORTANT: If you're uncertain about the spelling of a name (e.g., 'Thom' vs 'Tom', 'Sarah' vs 'Sara'), set confidence to 'low' to trigger spelling confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name the user provided",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "How confident you are about the name spelling",
                    },
                    "context": {
                        "type": "string",
                        "description": "Any additional context about how they introduced themselves",
                    },
                },
                "required": ["name", "confidence"],
            },
        },
        {
            "name": "get_news_digest",
            "type": "function",
            "description": "Fetch fresh material from the feeds configured on this device. "
            + _configured_topics_hint()
            + "Use it whenever the subject matches one of those topics - that source may well cover it, prices and niche subjects included. If nothing configured covers the subject, do not call this: use web_search instead when it is available. IMPORTANT: for headlines, provide a concise subject keyword so the tool can choose matching configured sources by keywords. CRITICAL: Before calling this tool, acknowledge VERY briefly (max 2 words), preferably exactly 'Checking.' Prefer user-provided location/team, otherwise rely on defaults.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["headlines", "weather", "sports"],
                        "description": "Digest type to fetch",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional topic for headlines (e.g., AI, elections)",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Keyword-style subject used for source selection (e.g., technology, politics, finance, project updates, sports). Strongly recommended for headlines.",
                    },
                    "location": {
                        "type": "string",
                        "description": "City/region for weather (e.g., Amsterdam)",
                    },
                    "sport": {
                        "type": "string",
                        "description": "Sports league or competition key. Common values: nfl, nba, mlb, nhl, epl, fifa.world. For association football World Cup requests use fifa.world, not epl or nfl.",
                    },
                    "team": {
                        "type": "string",
                        "description": "Optional team filter for sports",
                    },
                    "country": {
                        "type": "string",
                        "description": "Country code for regional headlines (e.g., US, NL)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code for feeds (e.g., en, nl)",
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "How many items to fetch",
                    },
                },
                "required": ["category"],
            },
        },
        {
            "name": "web_search",
            "type": "function",
            "description": (
                "Search the live internet and get a short spoken answer back. "
                "Use this ONLY when the answer cannot come from what you already "
                "know: something that happened or changed after your training, "
                "today's events, a current price or score, who holds a role now, "
                "a release date - or when the user asks you outright to look "
                "something up, search, or check online. For headlines, weather "
                "and sports call get_news_digest first and only search when that "
                "returns nothing useful. NEVER search for chat, opinions, jokes, "
                "general knowledge you already have, or anything about Billy, the "
                "user, or this household. Searching costs money and takes several "
                "seconds, so when in doubt, answer without it. Acknowledge VERY "
                "briefly (max 2 words, preferably 'Checking.') before calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to search for, as a self-contained question. "
                            "Resolve 'that' or 'he' from the conversation first, "
                            "and include a year or 'today' when recency matters."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "describe_scene",
            "type": "function",
            "description": "Capture one fresh image from Billy's camera. Use this when users ask what Billy can see, to look around, to check what's in front of him, or to describe a scene/object. Do not call this again to analyze an image that has already been attached in the conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Optional focus instruction (e.g., 'What objects are on the table?')",
                    },
                    "max_words": {
                        "type": "integer",
                        "minimum": 30,
                        "maximum": 180,
                        "description": "Upper word-count target for returned description",
                    },
                    "capture_timeout_seconds": {
                        "type": "number",
                        "minimum": 2,
                        "maximum": 15,
                        "description": "Timeout for camera capture command",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "search_local_knowledge",
            "type": "function",
            "description": "Search the files uploaded to this Billy: PDFs, spreadsheets, notes, manuals and household documents. "
            + _knowledge_topics_hint()
            + "Check here FIRST, before any feed or web search, whenever the "
            "question touches one of those topics or anything belonging to "
            "this household - their own manuals, paperwork, notes or things. "
            "Do not call it for questions about the wider world or about "
            "anything current: nothing here is newer than the day it was "
            "uploaded.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up in the uploaded local knowledge",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "How many matching snippets to return",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    ]


def get_user_tools() -> list[dict[str, Any]]:
    """Get user-specific tools (only available when not in guest mode)"""
    return [
        {
            "name": "store_memory",
            "type": "function",
            "description": "Store lasting preferences, facts, and interests that users VOLUNTARILY share. **CRITICAL: DO NOT STORE answers to YOUR OWN questions!** If you just asked a question, the answer is NOT a memory. Store ONLY when: (1) User volunteers info unprompted, OR (2) Info is NOT answering your question. Examples: WRONG: You: 'What cheese?' User: 'Gruyère' -> DO NOT STORE (answering your question). CORRECT: User: 'I love Gruyère cheese' -> DO STORE (volunteered). Call BEFORE responding with speech when appropriate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory": {
                        "type": "string",
                        "description": "The memory or fact to store about the user",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "How important this memory is",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "preference",
                            "fact",
                            "event",
                            "relationship",
                            "interest",
                        ],
                        "description": "Category of the memory",
                    },
                },
                "required": ["memory", "importance", "category"],
            },
        },
        {
            "name": "manage_profile",
            "type": "function",
            "description": "Manage user profile settings and preferences",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "switch_persona", "get_info"],
                        "description": "Action to perform on the profile",
                    },
                    "preferred_persona": {
                        "type": "string",
                        "description": "User's preferred Billy personality",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes about the user",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "switch_persona",
            "type": "function",
            "description": "Switch Billy's persona mid-session and acknowledge the change",
            "parameters": {
                "type": "object",
                "properties": {
                    "persona": {
                        "type": "string",
                        "description": "The persona to switch to",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for the persona switch",
                    },
                },
                "required": ["persona"],
            },
        },
    ]
