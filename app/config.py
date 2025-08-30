from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

# carries .env tokens, if dont exist uses fallbacks.
@dataclass(frozen=True)
class Settings: 
    google_client_secret_path: str = os.getenv("GOOGLE_CLIENT_SECRET_PATH", "google_client_secret.json")
    google_token_path: str = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
    gmail_from: str = os.getenv("GMAIL_FROM", "")
    llm_model: str = os.getenv("LLM_MODEL", "phi3:mini")
    default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/Chicago")

settings = Settings()
