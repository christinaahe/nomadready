from pathlib import Path

from dotenv import load_dotenv

# GOOGLE_PLACES_API_KEY and friends, as the app loads them
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
