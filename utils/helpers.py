"""Utilidades reutilizables para formateo y cálculos auxiliares."""

from datetime import datetime, timedelta, timezone

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


def format_number(n: int | float, decimals: int = 0) -> str:
    """1234567 → '1.234.567' (formato español)."""
    return f"{n:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def extract_time(datetime_str: str) -> str:
    """'2026-10-09 12:00:00' → '12:00:00'."""
    parts = datetime_str.split(" ")
    return parts[-1] if len(parts) > 1 else datetime_str

import re
from datetime import date

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

import discord
from config import Config


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