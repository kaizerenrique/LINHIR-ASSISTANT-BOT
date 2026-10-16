"""Utilidades reutilizables para formateo y cálculos auxiliares."""

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

import discord

from config import Config


# ===================================================================== #
#                          Ventanas de recursos                          #
# ===================================================================== #

# Zonas horarias de Albion Online (UTC): ventanas de recursos cada 6h.
_RESOURCE_WINDOWS_UTC = (0, 6, 12, 18)


def next_resource_window() -> str:
    """
    Calcula la próxima ventana de recursos de Albion.
    Retorna algo como: '18:00 UTC (en 5h 23m)'.
    """
    now = datetime.now(timezone.utc)
    next_hour = next(
        (h for h in _RESOURCE_WINDOWS_UTC if h > now.hour),
        _RESOURCE_WINDOWS_UTC[0],
    )

    target = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target.replace(hour=0) + timedelta(days=1)
        next_hour = 0

    delta = target - now
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    return f"`{next_hour:02d}:00 UTC` (en {hours}h {minutes}m)"


# ===================================================================== #
#                       Formateo de números y fechas                     #
# ===================================================================== #

def format_number(n: int | float, decimals: int = 0) -> str:
    """1234567 → '1.234.567' (formato español)."""
    return f"{n:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def extract_time(datetime_str: str) -> str:
    """'2026-10-09 12:00:00' → '12:00:00'."""
    parts = datetime_str.split(" ")
    return parts[-1] if len(parts) > 1 else datetime_str


def relative_time(ts_str: str) -> str:
    """
    Convierte 'Y-m-d H:i:s' (Laravel, sin TZ → asumimos UTC) en
    'hace X min' / 'hace Xh' / 'hace Xd'.
    """
    if not ts_str:
        return "fecha desconocida"

    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ts_str

    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "hace unos segundos"
    if seconds < 3600:
        return f"hace {seconds // 60} min"
    if seconds < 86400:
        return f"hace {seconds // 3600}h"
    return f"hace {seconds // 86400}d"


# ===================================================================== #
#                            Fecha de nacimiento                         #
# ===================================================================== #

# Aceptamos: DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD
_BIRTHDATE_PATTERNS = (
    re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$"),      # DD/MM/YYYY
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),              # YYYY-MM-DD
)


def parse_birthdate(raw: str) -> tuple[str | None, str | None]:
    """
    Parsea una fecha escrita por el usuario y la normaliza a YYYY-MM-DD.

    Retorna (iso_date, error):
        - Éxito: (iso_date, None)
        - Error: (None, "mensaje amigable")
    """
    if not raw or not raw.strip():
        return None, "No se especificó fecha."

    raw = raw.strip()

    day = month = year = None
    for pattern in _BIRTHDATE_PATTERNS:
        m = pattern.match(raw)
        if not m:
            continue
        g = m.groups()
        if len(g[0]) == 4:  # YYYY-MM-DD
            year, month, day = int(g[0]), int(g[1]), int(g[2])
        else:                # DD/MM/YYYY
            day, month, year = int(g[0]), int(g[1]), int(g[2])
        break

    if day is None:
        return None, (
            "Formato inválido. Usa `DD/MM/YYYY` "
            "(ej. `25/09/1995`) o `YYYY-MM-DD`."
        )

    # Validación de rango razonable
    today = date.today()
    if year < 1900 or year > today.year - 5:
        return None, (
            f"El año **{year}** no parece válido. "
            "Debe estar entre 1900 y " f"{today.year - 5}."
        )

    # Validación de fecha real (mes/día correctos)
    try:
        d = date(year, month, day)
    except ValueError:
        return None, f"La fecha `{raw}` no existe en el calendario."

    if d > today:
        return None, "La fecha de cumpleaños no puede estar en el futuro."

    return d.isoformat(), None


# ===================================================================== #
#                          Fecha + hora (UTC)                            #
# ===================================================================== #

# Formatos aceptados para fecha+hora (siempre interpretados como UTC)
_DATETIME_FORMATS = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
)


def parse_datetime(raw: str) -> tuple[str | None, str | None]:
    """
    Parsea una fecha+hora escrita por el usuario y devuelve ISO-8601 UTC.

    Retorna (iso_string, error):
        - Éxito: ("2026-10-15T20:30:00+00:00", None)
        - Error: (None, "mensaje amigable")
    """
    if not raw or not raw.strip():
        return None, "No se especificó fecha/hora."

    raw = raw.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat(), None
        except ValueError:
            continue

    return None, (
        "Formato inválido. Usa `DD/MM/YYYY HH:MM` "
        "(ej. `25/09/2026 20:30`) o `YYYY-MM-DD HH:MM`."
    )


# ===================================================================== #
#                              Permisos                                  #
# ===================================================================== #

async def is_officer(interaction: discord.Interaction) -> bool:
    """
    Verifica si el usuario que ejecuta el comando tiene permisos de oficial.

    - Tiene permiso `administrator` nativo de Discord → true
    - Tiene al menos uno de los roles listados en OFFICER_ROLE_IDS → true
    - En caso contrario → false
    """
    if not interaction.guild:
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    if not Config.OFFICER_ROLE_IDS:
        return False

    member_role_ids = {r.id for r in interaction.user.roles}
    return bool(member_role_ids & set(Config.OFFICER_ROLE_IDS))


# ===================================================================== #
#                          Sanitización de emojis                        #
# ===================================================================== #

# Rangos Unicode que aceptamos como emoji.
# Nota: no es exhaustivo, es un filtro defensivo para descartar texto plano.
_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # Banderas (regional indicators)
    (0x1F300, 0x1F5FF),  # Símbolos y pictogramas
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F680, 0x1F6FF),  # Transporte y mapas
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x1F3FB, 0x1F3FF),  # Tonos de piel
    (0x2600,  0x26FF),   # Símbolos misceláneos
    (0x2700,  0x27BF),   # Dingbats
    (0x2300,  0x23FF),   # Símbolos técnicos
    (0x2B00,  0x2BFF),   # Símbolos y flechas
    (0x2190,  0x21FF),   # Flechas
    (0x2900,  0x297F),   # Flechas suplementarias
    (0x3000,  0x303F),   # Símbolos CJK
    (0x3200,  0x32FF),   # CJK encerrados
    (0xFE00,  0xFE0F),   # Selectores de variación
    (0x1F000, 0x1F02F),  # Mahjong y fichas
    (0x1F0A0, 0x1F0FF),  # Cartas de poker
)

# Regex para custom emoji markup: <:name:id> o <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"^<(a?):(\w+):(\d+)>$")


def _is_emoji_codepoint(cp: int) -> bool:
    """Verifica si un codepoint pertenece a un rango emoji permitido."""
    if cp == 0x200D:  # ZWJ (Zero Width Joiner) — permitido dentro de secuencias
        return True
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def sanitize_emoji(raw: str | None) -> str | None:
    """
    Valida y limpia un emoji para uso seguro en Discord.

    Acepta:
      - Emoji Unicode estándar: 🛡️
      - Custom emoji markup de Discord: <:name:id> o <a:name:id>

    Devuelve el string limpio (Unicode o markup) o None si es inválido.

    Ejemplos:
        sanitize_emoji("🛡️")                → "🛡️"
        sanitize_emoji("👨‍👩‍👧‍👦")             → "👨‍👩‍👧‍👦"
        sanitize_emoji("<:shield:123456>")   → "<:shield:123456>"
        sanitize_emoji("<a:fire:999>")       → "<a:fire:999>"
        sanitize_emoji("hola")               → None
        sanitize_emoji(":shield:")           → None
    """
    if not raw:
        return None

    s = str(raw).strip()
    if not s:
        return None

    # -------- Custom emoji markup de Discord --------
    m = _CUSTOM_EMOJI_RE.match(s)
    if m:
        # Ya está en formato válido. No validamos contra el guild aquí
        # (eso lo hace el caller si necesita verificar pertenencia).
        return s

    # -------- Emoji Unicode --------
    s = unicodedata.normalize("NFC", s)

    if len(s) > 32:
        return None

    if any(c.isspace() or ord(c) < 0x20 for c in s):
        return None

    for c in s:
        if not _is_emoji_codepoint(ord(c)):
            return None

    has_base = any(
        ord(c) not in (0xFE0F, 0x200D)
        and not (0x1F3FB <= ord(c) <= 0x1F3FF)
        for c in s
    )
    if not has_base:
        return None

    return s