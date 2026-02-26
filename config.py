"""
config.py — Environment variable loading and validation.
All other modules import load_config() from here.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def load_config() -> dict:
    """
    Load and validate all required environment variables.
    Returns a typed config dictionary.
    Raises EnvironmentError if any required variable is missing.
    """
    required = [
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]

    config: dict = {}
    missing = []

    for key in required:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
        config[key] = val

    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials."
        )

    # Optional with defaults
    config["PRICE_DROP_THRESHOLD"] = float(
        os.environ.get("PRICE_DROP_THRESHOLD", "5.0")
    )
    # Larger chunks = fewer OpenAI calls = lower cost.
    # GPT-4o Mini context window is 128k tokens; 8000 chars ≈ 2000 tokens, well within limit.
    config["GEMINI_CHUNK_SIZE"] = int(
        os.environ.get("GEMINI_CHUNK_SIZE", "8000")
    )
    config["SHOP_LAT"] = float(os.environ.get("SHOP_LAT", "40.7569"))
    config["SHOP_LON"] = float(os.environ.get("SHOP_LON", "30.3783"))

    return config
