import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int = 0) -> int:
    """Lee una variable de entorno como int, con fallback seguro."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    # --- Discord ---
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
    DEV_GUILD_ID = _int_env("DEV_GUILD_ID") or None

    # --- Web API ---
    API_BASE_URL = os.getenv("API_BASE_URL", "https://linhir.online").rstrip("/")
    API_PREFIX = os.getenv("API_PREFIX", "/api/bot").rstrip("/")
    API_TIMEOUT = _int_env("API_TIMEOUT", 10)
    BOT_ACCESS_TOKEN = os.getenv("BOT_ACCESS_TOKEN", "").strip()

    # --- Personalización ---
    GUILD_NAME = os.getenv("GUILD_NAME", "Linhir")
    EMBED_COLOR = int(os.getenv("EMBED_COLOR", "0xC54B47"), 16)
    EMBED_ACCENT = int(os.getenv("EMBED_ACCENT", "0xD4B44A"), 16)

    # --- Roles de Discord (para /register) ---
    LINHIR_ROLE_ID = _int_env("LINHIR_ROLE_ID")
    PUBLICO_ROLE_ID = _int_env("PUBLICO_ROLE_ID")
    NICKNAME_PREFIX = os.getenv("NICKNAME_PREFIX", "[LH]")

    # --- Roles de oficial (para comandos administrativos) ---
    OFFICER_ROLE_IDS = [
        int(x.strip())
        for x in os.getenv("OFFICER_ROLE_IDS", "").split(",")
        if x.strip() and x.strip().isdigit()
    ]

    # --- Reportes automáticos ---
    REPORT_CHANNEL_ID = _int_env("REPORT_CHANNEL_ID")
    REPORT_HOUR_UTC = os.getenv("REPORT_HOUR_UTC", "12:00")

    # --- Recordatorios ---
    REMINDER_DM_DELAY = float(os.getenv("REMINDER_DM_DELAY", "1.2"))

    @classmethod
    def report_hour_and_minute(cls) -> tuple[int, int]:
        """Parsea 'HH:MM' y devuelve (hora, minuto). Fallback a 12:00."""
        try:
            h, m = cls.REPORT_HOUR_UTC.split(":")
            return int(h), int(m)
        except (ValueError, AttributeError):
            return 12, 0

    @classmethod
    def api_url(cls, path: str) -> str:
        """Compone URLs: /horario → https://linhir.online/api/bot/horario"""
        path = "/" + path.lstrip("/")
        return f"{cls.API_BASE_URL}{cls.API_PREFIX}{path}"

    @classmethod
    def auth_headers(cls) -> dict:
        """Headers con Bearer token de Sanctum."""
        return {
            "Authorization": f"Bearer {cls.BOT_ACCESS_TOKEN}",
            "Accept": "application/json",
        }

    @classmethod
    def validate(cls) -> None:
        """Valida que las variables críticas estén presentes."""
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN no configurado en .env")
        if not cls.BOT_ACCESS_TOKEN:
            raise ValueError(
                "BOT_ACCESS_TOKEN no configurado. Copia el token de la cuenta bot "
                "generado en el panel de administración de Linhir."
            )
        if not cls.API_BASE_URL:
            raise ValueError("API_BASE_URL no configurado en .env")
        if not cls.LINHIR_ROLE_ID:
            raise ValueError(
                "LINHIR_ROLE_ID no configurado en .env. "
                "Copia el ID del rol @Linhir desde Discord."
            )
        if not cls.PUBLICO_ROLE_ID:
            raise ValueError(
                "PUBLICO_ROLE_ID no configurado en .env. "
                "Copia el ID del rol @Publico desde Discord."
            )