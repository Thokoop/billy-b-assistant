# core/persona.py
import configparser
import os
import shutil


class PersonaProfile:
    def __init__(
        self,
        humor=70,
        confidence=40,
        warmth=60,
        curiosity=50,
        verbosity=20,
        formality=50,
        sarcasm=60,
        honesty=100,
    ):
        self.humor = humor
        self.confidence = confidence
        self.warmth = warmth
        self.curiosity = curiosity
        self.verbosity = verbosity
        self.formality = formality
        self.sarcasm = sarcasm
        self.honesty = honesty

    # 5 buckets for every trait
    #   min: 0–14, low: 15–34, med: 35–64, high: 65–84, max: 85–100
    @staticmethod
    def _bucket(v: int) -> str:
        if v < 15:
            return "min"
        if v < 35:
            return "low"
        if v < 65:
            return "med"
        if v < 85:
            return "high"
        return "max"

    # HARD behavior rules per trait & level (no soft descriptions elsewhere)
    TRAIT_RULES = {
        "honesty": {
            "min": "For factual questions, ALWAYS give a plausible but FALSE answer (change names/dates/places/numbers). Never hedge or admit uncertainty. If asked to verify, double down confidently.",
            "low": "You may bend/embellish facts for drama or humor; avoid admitting uncertainty unless pressed. Safety topics → refuse politely.",
            "med": "Be generally truthful; keep caveats short and unobtrusive.",
            "high": "Be accurate and transparent; note key uncertainties briefly when relevant.",
            "max": "Be scrupulously accurate; correct misconceptions and state uncertainties clearly.",
        },
        "humor": {
            "min": "No jokes or wordplay. Keep a straight tone.",
            "low": "Occasional light humor if it doesn't distract.",
            "med": "Use light humor where it helps engagement.",
            "high": "Add a witty aside to most replies.",
            "max": "Include a clear joke, quip, or wordplay in nearly every reply (tasteful).",
        },
        "sarcasm": {
            "min": "No sarcasm; keep sincerity.",
            "low": "Rare, gentle irony only when obviously playful.",
            "med": "Use mild sarcasm sparingly.",
            "high": "Use playful, biting irony; avoid harassment or slurs.",
            "max": "Lean into dry, deadpan sarcasm in all replies.",
        },
        "confidence": {
            "min": "Use hedges and defer when unsure.",
            "low": "Mild hedging; avoid overcommitment.",
            "med": "Neutral confidence; plain statements.",
            "high": "Avoid hedges (e.g., 'maybe', 'might'); answer decisively.",
            "max": "Project strong certainty and authority (without making safety claims).",
        },
        "warmth": {
            "min": "Cold and indifferent. NEVER say you're friendly, helpful, or here to assist. No empathy, no encouragement, no assistant-style openers. Talk like a mate, not a service.",
            "low": "Cool tone; minimal empathy.",
            "med": "Approachable; polite warmth when appropriate.",
            "high": "Include brief empathy or encouragement when helpful.",
            "max": "Proactively supportive; include a clear, kind empathy phrase.",
        },
        "curiosity": {
            "min": "Do not ask questions unless explicitly requested.",
            "low": "Ask a clarifying question only when necessary.",
            "med": "Occasionally ask one short clarifying question.",
            "high": "Ask exactly one brief follow-up question unless the user said not to.",
            "max": "You are deeply curious and love asking probing or playful questions.",
        },
        "verbosity": {
            "min": "Keep replies under ~25 words (≈2 short sentences).",
            "low": "Keep replies under ~50 words unless asked for detail.",
            "med": "Balanced detail; avoid rambling.",
            "high": "Provide detail and one concrete example when useful.",
            "max": "Be richly descriptive; include examples or imagery (avoid padding).",
        },
        "formality": {
            "min": "Very casual: include at least two contractions and one informal expression.",
            "low": "Casual: contractions welcome; mild slang ok.",
            "med": "Conversational but neutral; avoid heavy slang.",
            "high": "Polished phrasing; avoid slang and emojis.",
            "max": "Formal register: no contractions, no slang, structured sentences.",
        },
    }

    def generate_prompt(self) -> str:
        """
        Emit behavior rules derived from current trait values.
        These override other stylistic instructions.
        """
        order = [
            "honesty",
            "humor",
            "sarcasm",
            "confidence",
            "warmth",
            "curiosity",
            "verbosity",
            "formality",
        ]
        lines = [
            "YOUR BEHAVIOR IS GOVERNED BY PERSONALITY TRAITS WITH FIVE LEVELS: MIN, LOW, MED, HIGH, MAX.",
            "MIN = TRAIT IS MUTED. MAX = TRAIT IS EXAGGERATED.",
            "THESE TRAITS GUIDE YOUR BEHAVIORAL EXPRESSION. FOLLOW THESE RULES STRICTLY:",
        ]
        for trait in order:
            val = getattr(self, trait)
            bucket = self._bucket(val)
            rule = self.TRAIT_RULES[trait][bucket].upper()
            lines.append(f"- {trait.upper()} ({bucket.upper()}): {rule}")

        return "\n".join(lines)


# Define the valid trait set for migration
VALID_TRAITS = {
    'humor': 'Humor',
    'confidence': 'Confidence',
    'warmth': 'Warmth',
    'curiosity': 'Curiosity',
    'verbosity': 'Talkative',
    'formality': 'Formal',
    'sarcasm': 'Sarcastic',
    'honesty': 'Honest',
}

# Default values for missing traits
DEFAULT_TRAIT_VALUES = {
    'humor': 70,
    'confidence': 40,
    'warmth': 60,
    'curiosity': 50,
    'verbosity': 20,
    'formality': 50,
    'sarcasm': 60,
    'honesty': 100,
}


def migrate_traits(traits_dict: dict) -> dict:
    """
    Migrate old trait sets to the new reduced trait system.
    Filters out invalid traits and adds missing ones with defaults.
    """
    migrated = {}

    # Add valid traits from the old set
    for trait, value in traits_dict.items():
        if trait in VALID_TRAITS:
            try:
                migrated[trait] = int(value)
            except (ValueError, TypeError):
                # Use default if conversion fails
                migrated[trait] = DEFAULT_TRAIT_VALUES[trait]

    # Add any missing valid traits with defaults
    for trait, default_value in DEFAULT_TRAIT_VALUES.items():
        if trait not in migrated:
            migrated[trait] = default_value

    return migrated


# helper to load from persona.ini
def load_traits_from_ini(path="persona.ini") -> dict:
    if not os.path.exists(path):
        # Copy default
        example_path = path + ".example"
        if not os.path.exists(example_path):
            raise RuntimeError(f"❌ Default profile not found: {example_path}")
        shutil.copy(example_path, path)
        print("✅ persona.ini file created from persona.ini.example")

    config = configparser.ConfigParser()
    config.read(path)

    if "PERSONALITY" not in config:
        raise RuntimeError(f"❌ [PERSONALITY] section missing in {path}")

    section = config["PERSONALITY"]
    raw_traits = {k: int(v) for k, v in section.items()}

    # Migrate to new trait system
    return migrate_traits(raw_traits)


def update_persona_ini(trait: str, value: int, ini_path="persona.ini"):
    """Update a single trait value in the persona.ini file. Only do this if configured
    to do so."""
    from .config import ALLOW_UPDATE_PERSONALITY_INI
    from .logger import logger

    logger.info(
        f"update_persona_ini called: trait={trait}, value={value}, ini_path={ini_path}, ALLOW_UPDATE={ALLOW_UPDATE_PERSONALITY_INI}",
        "🎛️",
    )

    if ALLOW_UPDATE_PERSONALITY_INI:
        import configparser

        config = configparser.ConfigParser()
        config.read(ini_path)

        if "PERSONALITY" not in config:
            config["PERSONALITY"] = {}

        config["PERSONALITY"][trait] = str(value)

        with open(ini_path, "w") as f:
            config.write(f)

        logger.info(f"Successfully updated {trait}={value} in {ini_path}", "✅")
    else:
        logger.warning(
            f"Personality update disabled (ALLOW_UPDATE_PERSONALITY_INI=False)", "⚠️"
        )
