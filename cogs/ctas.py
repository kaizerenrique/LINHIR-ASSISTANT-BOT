"""Cog de CTAs: creación, inscripción, ausencias, pagos, anuncios y voz.

Arquitectura de los botones:
  - Los mensajes de CTA contienen botones con custom_id dinámicos:
      linhir:cta:join:{id}
      linhir:cta:absent:{id}
      linhir:cta:refresh:{id}

  - Como los custom_id varían por CTA, NO se pueden registrar con
    `bot.add_view()` (esa API requiere custom_id únicos y constantes).

  - En su lugar, se usa un listener global `on_interaction` que captura
    cualquier botón cuyo custom_id coincida con el patrón.

  - Cada handler hace `interaction.response.defer()` ANTES de llamar a
    la API de Laravel, para evitar el timeout de 3 s de Discord.

  - El regex acepta tanto `linhir:cta:*` (lo que publica la web y este
    cog) como `cta:*` (formato usado por versiones anteriores del bot),
    garantizando retrocompatibilidad con mensajes ya publicados.

Nuevas funcionalidades:
  - Anuncios diferidos: El bot revisa CTAs sin anunciar y las publica.
  - Canales de voz automáticos: Crea el canal 10 min antes y lo borra al quedar vacío.
  - Medición de asistencia: Cuenta los segundos que un usuario pasa en el canal de voz.
"""

import logging
import re
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import format_number, is_officer, parse_datetime, sanitize_emoji

logger = logging.getLogger(__name__)


# ===================================================================== #
#              Regex de custom_ids (web + bot, retrocompatible)         #
# ===================================================================== #

CTA_JOIN_RE    = re.compile(r"^(?:linhir:)?cta:join:(\d+)$")
CTA_ABSENT_RE  = re.compile(r"^(?:linhir:)?cta:absent:(\d+)$")
CTA_REFRESH_RE = re.compile(r"^(?:linhir:)?cta:refresh:(\d+)$")

# Límite duro de Discord para field.value en un embed
_FIELD_VALUE_MAX = 1024

# Cache en memoria para rastrear cuándo un usuario entra al canal de voz
# Estructura: { cta_id: { user_id: join_datetime } }
VOICE_SESSIONS: dict[int, dict[int, datetime]] = {}


# ===================================================================== #
#                     Autocompletado de composiciones                   #
# ===================================================================== #

async def composition_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocompleta composiciones activas filtradas por `current`."""
    try:
        result, status = await APIClient.get_compositions()
    except (AuthenticationError, AuthorizationError):
        return []

    if status != 200 or not result.get("success"):
        return []

    comps = result.get("compositions", [])
    return [
        app_commands.Choice(name=c["name"], value=str(c["id"]))
        for c in comps
        if current.lower() in c["name"].lower()
    ][:25]


# ===================================================================== #
#                    Select de armas (mensaje efímero)                  #
# ===================================================================== #

class WeaponSelect(discord.ui.Select):
    """
    Select para elegir un arma. Acepta un subconjunto (chunk) de armas.

    Discord permite máximo 25 opciones por Select. Si la composición
    tiene más armas, se crean varios Selects en filas distintas.
    """

    def __init__(
        self,
        cta_id: int,
        weapons_chunk: list[dict],
        occupancy: dict,
        author_id: int,
        row: int,
        chunk_label: str = "",
    ):
        self.cta_id = cta_id
        self.occupancy = occupancy
        self.author_id = author_id

        options: list[discord.SelectOption] = []
        for w in weapons_chunk:
            wid = w["id"]
            taken_by = occupancy.get(str(wid)) or occupancy.get(wid)

            icon_raw = w.get("weapon_icon")
            icon = sanitize_emoji(icon_raw) if icon_raw else None

            label = w["weapon_name"][:90]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=(
                        f"Ocupada por {taken_by}" if taken_by else "Libre"
                    )[:100],
                    value=str(wid),
                    emoji=icon,
                )
            )

        placeholder = "Elige tu arma..."
        if chunk_label:
            placeholder = f"Elige tu arma ({chunk_label})..."

        super().__init__(
            placeholder=placeholder[:150],
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien pidió unirse puede seleccionar.", ephemeral=True
            )
            return

        weapon_id = int(self.values[0])

        taken_by = self.occupancy.get(str(weapon_id)) or self.occupancy.get(weapon_id)
        if taken_by:
            await interaction.response.send_message(
                f"⚠️ Esa arma ya está ocupada por **{taken_by}**. Elige otra.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            result, status = await APIClient.cta_join(
                cta_id=self.cta_id,
                discord_user_id=str(interaction.user.id),
                user_name=interaction.user.display_name,
                weapon_id=weapon_id,
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo procesar tu inscripción.')}",
                ephemeral=True,
            )
            return

        weapon_name = "—"
        for w in self.view.all_weapons:
            if w["id"] == weapon_id:
                weapon_name = w["weapon_name"]
                break

        embed = discord.Embed(
            title="✅ Inscripción confirmada",
            description=(
                f"Te uniste a la CTA con el arma **{weapon_name}**.\n"
                f"Recibirás un DM de recordatorio antes del inicio."
            ),
            color=0x5CCB45,
        )
        await interaction.edit_original_response(embed=embed, view=None)

        await self.view.refresh_public_message()


# ===================================================================== #
#              Vista con Select(s) de armas + botón Cancelar            #
# ===================================================================== #

class WeaponSelectView(discord.ui.View):
    """
    Vista efímera con uno o varios Selects de armas.

    Si la composición tiene >25 armas, se crean múltiples Selects en
    filas separadas (hasta 4 filas = 100 armas). El botón Cancelar
    ocupa la última fila disponible.
    """

    MAX_WEAPONS_PER_SELECT = 25
    MAX_ROWS_FOR_SELECTS = 4

    def __init__(
        self,
        cta_id: int,
        weapons: list[dict],
        occupancy: dict,
        author_id: int,
        channel_id: int,
        message_id: int,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.cta_id = cta_id
        self.all_weapons = weapons
        self.channel_id = channel_id
        self.message_id = message_id
        # Se asigna externamente desde el handler: `view.bot = interaction.client`
        self.bot = None

        chunks = [
            weapons[i:i + self.MAX_WEAPONS_PER_SELECT]
            for i in range(0, len(weapons), self.MAX_WEAPONS_PER_SELECT)
        ]

        if len(chunks) > self.MAX_ROWS_FOR_SELECTS:
            logger.warning(
                "Composición con %d armas: mostrando solo %d (límite de filas en Discord)",
                len(weapons),
                self.MAX_ROWS_FOR_SELECTS * self.MAX_WEAPONS_PER_SELECT,
            )
            chunks = chunks[:self.MAX_ROWS_FOR_SELECTS]

        for i, chunk in enumerate(chunks):
            chunk_label = ""
            if len(chunks) > 1:
                start = i * self.MAX_WEAPONS_PER_SELECT + 1
                end = start + len(chunk) - 1
                chunk_label = f"{start}-{end}"
            self.add_item(
                WeaponSelect(
                    cta_id=cta_id,
                    weapons_chunk=chunk,
                    occupancy=occupancy,
                    author_id=author_id,
                    row=i,
                    chunk_label=chunk_label,
                )
            )

        cancel_row = min(len(chunks), 4)
        cancel_btn = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            row=cancel_row,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="❌ Inscripción cancelada.", embed=None, view=None
        )

    async def refresh_public_message(self):
        """Recarga la CTA y edita el mensaje público original."""
        if not self.message_id or not self.bot:
            return

        try:
            result, status = await APIClient.get_cta(self.cta_id)
            if status != 200 or not result.get("success"):
                return

            channel = self.bot.get_channel(self.channel_id)
            if channel is None:
                return

            msg = await channel.fetch_message(self.message_id)
            embed = build_cta_embed(result["cta"])
            new_view = CtaAnnouncementView(cta_id=self.cta_id)
            await msg.edit(embed=embed, view=new_view)
        except Exception as exc:
            logger.warning(
                "No se pudo refrescar el mensaje público de la CTA #%s: %s",
                self.cta_id, exc,
            )


# ===================================================================== #
#             Vista pública: Unirse / Ausente / Refresh                 #
# ===================================================================== #

class CtaAnnouncementView(discord.ui.View):
    """
    Vista pública del mensaje de CTA.

    IMPORTANTE: esta vista es SOLO VISUAL. Los custom_id de los botones
    son dinámicos (contienen el ID de la CTA), por lo que no se puede
    registrar con `bot.add_view()`. La lógica real está en el listener
    `on_interaction` de `CtasCog`.

    Los callbacks definidos aquí NUNCA se ejecutan (la vista no está
    registrada con add_view). Se definen únicamente para que los
    botones se rendericen con el estilo, emoji y custom_id correctos.
    """

    def __init__(self, cta_id: int, timeout: int | None = None):
        super().__init__(timeout=timeout)
        self.cta_id = cta_id

        # custom_ids con formato "linhir:cta:*" (compatible con la web)
        self.join_btn.custom_id    = f"linhir:cta:join:{cta_id}"
        self.absent_btn.custom_id  = f"linhir:cta:absent:{cta_id}"
        self.refresh_btn.custom_id = f"linhir:cta:refresh:{cta_id}"

    @discord.ui.button(
        label="Unirse",
        style=discord.ButtonStyle.success,
        emoji="⚔️",
        row=0,
    )
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Callback no ejecutado: la lógica está en on_interaction
        pass

    @discord.ui.button(
        label="Ausente",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        row=0,
    )
    async def absent_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Callback no ejecutado: la lógica está en on_interaction
        pass

    @discord.ui.button(
        label="🔄",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Callback no ejecutado: la lógica está en on_interaction
        pass


# ===================================================================== #
#             Vista efímera: Selección de hora de anuncio               #
# ===================================================================== #

class AnnouncementTimeView(discord.ui.View):
    """Vista para que el oficial decida cuándo se envía el ping."""

    def __init__(self, cta_id: int, channel_id: int, role_id: int, author_id: int, start_time: datetime, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.cta_id = cta_id
        self.channel_id = channel_id
        self.role_id = role_id
        self.start_time = start_time
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo el creador puede elegir.", ephemeral=True)
            return False
        return True

    async def _process_choice(self, interaction: discord.Interaction, minutes_before: int | None):
        await interaction.response.defer(ephemeral=True)

        now = datetime.now(timezone.utc)

        if minutes_before is None:
            announce_at = now
        else:
            announce_at = self.start_time - timedelta(minutes=minutes_before)
            # Si el momento calculado ya pasó, anunciar ya
            if announce_at < now:
                announce_at = now

        try:
            result, status = await APIClient.update_cta_announce(
                self.cta_id, announce_at.isoformat()
            )

            if status == 200 and result.get("success"):
                if minutes_before is None:
                    msg = "✅ El anuncio se enviará inmediatamente en unos segundos."
                elif announce_at == now:
                    msg = ("⚠️ El momento calculado ya había pasado. "
                           "El anuncio se enviará **inmediatamente** en unos segundos.")
                else:
                    ts = int(announce_at.timestamp())
                    msg = f"✅ El anuncio se enviará automáticamente <t:{ts}:R>."

                embed = discord.Embed(
                    title="⏳ Anuncio Programado",
                    description=msg,
                    color=Config.EMBED_COLOR,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                error = result.get("reason") or result.get("error") or "Error desconocido"
                await interaction.followup.send(
                    f"❌ Error al programar: {error}", ephemeral=True
                )
        except Exception as exc:
            logger.error("Error programando anuncio: %s", exc, exc_info=exc)
            await interaction.followup.send(
                "❌ Error de conexión con el servidor.", ephemeral=True
            )

        self.stop()

    @discord.ui.button(label="Inmediato", style=discord.ButtonStyle.primary)
    async def btn_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._process_choice(interaction, None)

    @discord.ui.button(label="15 min antes", style=discord.ButtonStyle.secondary)
    async def btn_15m(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._process_choice(interaction, 15)

    @discord.ui.button(label="1 hora antes", style=discord.ButtonStyle.secondary)
    async def btn_1h(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._process_choice(interaction, 60)

    @discord.ui.button(label="2 horas antes", style=discord.ButtonStyle.secondary)
    async def btn_2h(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._process_choice(interaction, 120)
    

# ===================================================================== #
#                        Constructor de embed                           #
# ===================================================================== #

def _build_weapons_field(
    weapons: list[dict],
    occupancy: dict[int, str],
) -> tuple[str, int]:
    """
    Construye el texto del field "Armas y asignación" respetando el
    límite de 1024 caracteres de Discord.

    Retorna: (texto, cantidad_ocupadas)
    """
    if not weapons:
        return ("*Sin armas definidas*", 0)

    lineas: list[str] = []
    for w in weapons:
        icon = sanitize_emoji(w.get("weapon_icon")) or "•"
        name = w.get("weapon_name", "—")
        taken = occupancy.get(w["id"])
        if taken:
            lineas.append(f"{icon} **{name}** — 🔒 {taken}")
        else:
            lineas.append(f"{icon} {name} — ⚪ *libre*")

    # Acumular líneas hasta acercarse a 1024 con margen para el sufijo
    suffix_margin = 60
    max_len = _FIELD_VALUE_MAX - suffix_margin
    out: list[str] = []
    total_len = 0
    shown = 0

    for linea in lineas:
        extra = (1 if shown > 0 else 0) + len(linea)
        if total_len + extra > max_len:
            break
        out.append(linea)
        total_len += extra
        shown += 1

    texto = "\n".join(out)
    if shown < len(lineas):
        texto += f"\n… y **{len(lineas) - shown}** más (ver en la web)"

    # Red de seguridad final
    if len(texto) > _FIELD_VALUE_MAX:
        texto = texto[: _FIELD_VALUE_MAX - 1] + "…"

    return (texto, len(occupancy))


def build_cta_embed(cta: dict) -> discord.Embed:
    """Construye el embed público de una CTA con ocupación de armas."""
    cta_id = cta.get("id", "?")
    title = (cta.get("title") or "—")[:250]
    location = cta.get("location") or "—"
    status = cta.get("status", "scheduled")
    paid = cta.get("paid", False)
    composition = cta.get("composition") or {}
    weapons = composition.get("weapons") or []
    participants = cta.get("participants") or []
    
    # Nuevos campos
    visibility = cta.get("visibility", "linhir")
    loot_type = cta.get("loot_type", "taxed_split")

    color_map = {
        "scheduled": Config.EMBED_COLOR,
        "cancelled": 0xFF5252,
        "completed": 0x5CCB45,
    }
    color = color_map.get(status, Config.EMBED_COLOR)
    
    vis_icon = "🤝" if visibility == "alliance" else "🛡️"
    vis_txt = "Alianza HRL" if visibility == "alliance" else "Solo Linhir"
    
    loot_map = {
        "full_split": "Full Split (Sin impuestos)",
        "taxed_split": "Split con Impuesto (15%)",
        "pro_guild": "Pro-Gremio (Todo al banco)"
    }
    loot_txt = loot_map.get(loot_type, loot_type)

    embed = discord.Embed(
        title=f"⚔️ {title}",
        description=(
            f"📍 **{location}**\n"
            f"{vis_icon} **{vis_txt}** | 💰 **{loot_txt}**\n"
            f"🎭 Composición: **{composition.get('name', '—')}**"
        ),
        color=color,
    )

    # Fechas
    start_iso = cta.get("start_time")
    end_iso = cta.get("end_time")
    if start_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            ts = int(start_dt.timestamp())
            embed.add_field(name="⏰ Inicio", value=f"<t:{ts}:F>\n(<t:{ts}:R>)", inline=True)
        except (ValueError, TypeError):
            embed.add_field(name="⏰ Inicio", value=start_iso, inline=True)

    if end_iso:
        try:
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            ts = int(end_dt.timestamp())
            embed.add_field(name="🏁 Fin", value=f"<t:{ts}:t>", inline=True)
        except (ValueError, TypeError):
            embed.add_field(name="🏁 Fin", value=end_iso, inline=True)

    # Estado
    if status == "cancelled":
        embed.add_field(name="Estado", value="❌ **Cancelada**", inline=True)
    elif status == "completed":
        estado_txt = "✅ Completada"
        if paid: estado_txt += " · 💵 Pagada"
        embed.add_field(name="Estado", value=estado_txt, inline=True)
    elif paid:
        embed.add_field(name="Estado", value="💵 **Pagada**", inline=True)

    # Ocupación por arma
    occupancy: dict[int, str] = {}
    for p in participants:
        if p.get("status") == "confirmed" and p.get("composition_weapon_id"):
            occupancy[p["composition_weapon_id"]] = p["user_name"]

    if weapons:
        armas_text, ocupadas = _build_weapons_field(weapons, occupancy)
        embed.add_field(
            name=f"🎭 Armas ({ocupadas}/{len(weapons)} ocupadas)",
            value=armas_text,
            inline=False,
        )

    # Confirmados / Ausentes
    confirmed = [p for p in participants if p.get("status") == "confirmed"]
    absent    = [p for p in participants if p.get("status") == "absent"]

    if confirmed:
        names = ", ".join(p["user_name"] for p in confirmed[:15])
        if len(confirmed) > 15:
            names += f" (+{len(confirmed) - 15})"
        embed.add_field(
            name=f"✅ Confirmados ({len(confirmed)})",
            value=names[:1024],
            inline=False,
        )

    if absent:
        names = ", ".join(p["user_name"] for p in absent[:10])
        if len(absent) > 10:
            names += f" (+{len(absent) - 10})"
        embed.add_field(
            name=f"🚫 Ausentes ({len(absent)})",
            value=names[:1024],
            inline=False,
        )

    # Imagen (si existe)
    image_url = cta.get("image_url")
    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=f"CTA #{cta_id} · {Config.GUILD_NAME}")
    return embed


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class CtasCog(commands.Cog):
    """Comandos y ciclo de vida de las CTAs del gremio."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ================================================================== #
    #                     Ciclo de vida                                   #
    # ================================================================== #

    async def cog_load(self):
        await self._verify_cta_publications()
        self.reminder_loop.start()
        self.announcement_loop.start()
        self.voice_channel_loop.start()
        self.empty_channel_cleanup_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()
        self.announcement_loop.cancel()
        self.voice_channel_loop.cancel()
        self.empty_channel_cleanup_loop.cancel()

    async def _verify_cta_publications(self):
        try:
            result, status = await APIClient.get_active_ctas()
        except (AuthenticationError, AuthorizationError):
            return

        if status != 200 or not result.get("success"):
            return

        ctas = result.get("ctas", [])
        with_ids = sum(1 for c in ctas if c.get("discord_message_id") and c.get("discord_channel_id"))
        logger.info(
            "CTAs activas: %d · con IDs de Discord: %d/%d (on_interaction)",
            len(ctas), with_ids, len(ctas),
        )

    # ================================================================== #
    #                 LISTENER GLOBAL: BOTONES DE CTA                     #
    # ================================================================== #

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

        logger.info("CTA interaction: custom_id=%r user=%s", custom_id, interaction.user)

        m = CTA_JOIN_RE.match(custom_id)
        if m:
            await self._handle_join_btn(interaction, int(m.group(1)))
            return

        m = CTA_ABSENT_RE.match(custom_id)
        if m:
            await self._handle_absent_btn(interaction, int(m.group(1)))
            return

        m = CTA_REFRESH_RE.match(custom_id)
        if m:
            await self._handle_refresh_btn(interaction, int(m.group(1)))
            return

    # ================================================================== #
    #                    HANDLERS DE BOTONES                              #
    # ================================================================== #

    async def _handle_join_btn(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_cta(cta_id)
        except Exception as exc:
            logger.exception("join_btn: error get_cta %s", cta_id)
            await interaction.followup.send(f"❌ Error de conexión: {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send("❌ No se pudo cargar la CTA.", ephemeral=True)
            return

        cta = result["cta"]
        if cta.get("status") != "scheduled":
            await interaction.followup.send("❌ Esta CTA ya no está activa.", ephemeral=True)
            return

        weapons = (cta.get("composition") or {}).get("weapons") or []
        occupancy = {str(p["composition_weapon_id"]): p["user_name"] for p in cta.get("participants") or [] if p.get("status") == "confirmed" and p.get("composition_weapon_id")}

        if not weapons:
            await interaction.followup.send("⚠️ Esta CTA no tiene armas.", ephemeral=True)
            return

        view = WeaponSelectView(
            cta_id=cta_id,
            weapons=weapons,
            occupancy=occupancy,
            author_id=interaction.user.id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id if interaction.message else 0,
        )
        view.bot = interaction.client

        await interaction.followup.send("Selecciona el arma con la que participarás:", view=view, ephemeral=True)

    async def _handle_absent_btn(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True)

        try:
            result, status = await APIClient.cta_absent(cta_id, str(interaction.user.id))
        except Exception as exc:
            await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(f"❌ {result.get('reason', 'Error al ausentarse.')}", ephemeral=True)
            return

        await interaction.followup.send("🚫 Marcado como ausente. Se ha liberado tu arma.", ephemeral=True)
        await self._refresh_public_cta_message(interaction, cta_id)

    async def _handle_refresh_btn(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer()
        await self._refresh_public_cta_message(interaction, cta_id)

    async def _refresh_public_cta_message(self, interaction: discord.Interaction, cta_id: int):
        if not interaction.message:
            return

        try:
            result, status = await APIClient.get_cta(cta_id)
            if status == 200 and result.get("success"):
                embed = build_cta_embed(result["cta"])
                new_view = CtaAnnouncementView(cta_id=cta_id)
                await interaction.message.edit(embed=embed, view=new_view)
        except Exception as exc:
            logger.warning("No se pudo refrescar CTA #%s: %s", cta_id, exc)

    # ================================================================== #
    #                     Tareas en segundo plano                        #
    # ================================================================== #

    @tasks.loop(seconds=60)
    async def reminder_loop(self):
        try:
            result, status = await APIClient.get_pending_reminders()
            if status != 200 or not result.get("success"): return

            for cta in result.get("ctas", []):
                cta_id = cta["id"]
                participants = cta.get("confirmed_participants") or []
                
                if not participants:
                    await APIClient.mark_reminder_sent(cta_id)
                    continue

                start_iso = cta.get("start_time")
                start_fmt = start_iso
                try:
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    start_fmt = f"<t:{int(start_dt.timestamp())}:R>"
                except (ValueError, TypeError, AttributeError):
                    pass

                body = f"🔔 **Recordatorio de CTA**\n\n**{cta.get('title', '—')}**\n📍 {cta.get('location', '—')}\n⏰ Inicia {start_fmt}\n\n¡No olvides asistir!"

                for p in participants:
                    uid = p.get("discord_user_id")
                    if not uid: continue
                    try:
                        user = await self.bot.fetch_user(int(uid))
                        await user.send(body)
                    except (discord.Forbidden, discord.NotFound):
                        pass
                    except discord.HTTPException as exc:
                        logger.warning("DM fallo para %s: %s", uid, exc)

                await APIClient.mark_reminder_sent(cta_id)
                logger.info("Recordatorio CTA #%s enviado", cta_id)

        except Exception as exc:
            logger.exception("Error en reminder_loop: %s", exc)

    @tasks.loop(seconds=60)
    async def announcement_loop(self):
        """Revisa CTAs pendientes de anuncio y las publica."""
        try:
            result, status = await APIClient.get_pending_announcements()
            if status != 200 or not result.get("success"): return
            
            for cta in result.get("ctas", []):
                cta_id = cta["id"]
                vis = cta.get("visibility", "linhir")
                channel_id = Config.CTA_ANNOUNCE_CHANNEL_ID if vis == "linhir" else Config.CTA_CONTENT_CHANNEL_ID
                role_id = cta.get("discord_role_id")

                if not channel_id:
                    logger.error("Falta canal configurado para visibilidad %s", vis)
                    continue

                channel = self.bot.get_channel(channel_id)
                if not channel: continue

                embed = build_cta_embed(cta)
                view = CtaAnnouncementView(cta_id=cta_id)
                content = f"<@&{role_id}>" if role_id else None
                allowed = discord.AllowedMentions(roles=True) if role_id else discord.AllowedMentions.none()

                try:
                    msg = await channel.send(content=content, embed=embed, view=view, allowed_mentions=allowed)
                    await APIClient.mark_announced(cta_id, str(msg.id), str(channel.id))
                    logger.info("CTA #%s anunciada en #%s", cta_id, channel.name)
                except Exception as exc:
                    logger.error("Error publicando CTA #%s: %s", cta_id, exc)
                
                await asyncio.sleep(2) # Evitar rate limit
        except Exception as exc:
            logger.exception("Error en announcement_loop: %s", exc)

    @tasks.loop(seconds=60)
    async def voice_channel_loop(self):
        """Crea canales de voz 10 min antes del inicio."""
        try:
            result, status = await APIClient.get_active_ctas()
            if status != 200 or not result.get("success"): return
            
            now = datetime.now(timezone.utc)
            for cta in result.get("ctas", []):
                if cta.get("discord_voice_channel_id"): continue
                
                start_dt = datetime.fromisoformat(cta["start_time"].replace("Z", "+00:00"))
                diff = (start_dt - now).total_seconds() / 60.0

                if 0 <= diff <= 12:
                    await self._create_voice_channel(cta)
        except Exception as exc:
            logger.exception("Error en voice_channel_loop: %s", exc)

    def _flush_voice_sessions(self, cta_id: int):
        """Descarta sesiones de voz huérfanas de una CTA."""
        VOICE_SESSIONS.pop(cta_id, None)

    @tasks.loop(minutes=1)
    async def empty_channel_cleanup_loop(self):
        """
        Borra canales de voz de CTA en los siguientes casos:
          - El canal está VACÍO (sin humanos).
          - La CTA ya TERMINÓ (end_time < now).
          - La CTA fue CANCELADA o COMPLETADA.
        """
        try:
            result, status = await APIClient.get_ctas_with_voice()
            if status != 200 or not result.get("success"):
                return

            now = datetime.now(timezone.utc)

            for cta in result.get("ctas", []):
                vc_id   = cta.get("discord_voice_channel_id")
                cta_id  = cta.get("id")
                if not vc_id:
                    continue

                # --- Motivos de borrado ---
                reason_to_delete = None

                # 1. Estado terminal
                if cta.get("status") in ("cancelled", "completed"):
                    reason_to_delete = f"CTA {cta['status']}"

                # 2. Actividad terminada
                else:
                    end_iso = cta.get("end_time")
                    if end_iso:
                        try:
                            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                            # Margen de 5 min después del end_time para no cortar
                            # partidas que se alargan
                            if now > end_dt + timedelta(minutes=5):
                                reason_to_delete = "Actividad terminada"
                        except (ValueError, TypeError):
                            pass

                # 3. Canal vacío
                channel = self.bot.get_channel(int(vc_id))
                if channel is None:
                    # Canal borrado manualmente en Discord → limpiar en BD
                    await APIClient.clear_voice_channel(cta_id)
                    self._flush_voice_sessions(cta_id)
                    logger.info(
                        "Canal %s de CTA #%s no existe en Discord. ID limpiado.",
                        vc_id, cta_id,
                    )
                    continue

                if reason_to_delete is None:
                    humans = [m for m in channel.members if not m.bot]
                    if len(humans) == 0:
                        reason_to_delete = "Canal vacío"

                if reason_to_delete is None:
                    continue

                # --- Borrar ---
                try:
                    await channel.delete(reason=reason_to_delete)
                    await APIClient.clear_voice_channel(cta_id)
                    logger.info(
                        "Canal de voz %s de CTA #%s eliminado (%s)",
                        vc_id, cta_id, reason_to_delete,
                    )
                except discord.NotFound:
                    # Ya no existe → limpiar en BD
                    await APIClient.clear_voice_channel(cta_id)
                except discord.HTTPException as exc:
                    logger.warning(
                        "No se pudo borrar el canal %s (CTA #%s): %s",
                        vc_id, cta_id, exc,
                    )

                await asyncio.sleep(1)  # anti rate-limit

        except Exception as exc:
            logger.exception("Error en empty_channel_cleanup_loop: %s", exc)

    async def _create_voice_channel(self, cta: dict):
        cta_id = cta["id"]
        vis = cta.get("visibility", "linhir")
        guild = self._pick_guild()
        if not guild:
            logger.error("_create_voice_channel: no se encontró el guild de Linhir")
            return

        # Categoría destino (si está configurada)
        category = guild.get_channel(Config.VOICE_CATEGORY_ID) if Config.VOICE_CATEGORY_ID else None
        if Config.VOICE_CATEGORY_ID and category is None:
            logger.warning(
                "VOICE_CATEGORY_ID=%s no encontrado en el guild. "
                "El canal se creará en la raíz.",
                Config.VOICE_CATEGORY_ID,
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False),
            guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True),
        }

        role_id = Config.HRL_ROLE_ID if vis == "alliance" else Config.LINHIR_ROLE_ID
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(connect=True)

        try:
            vc = await guild.create_voice_channel(
                name=f"🔊 {cta.get('title', 'CTA')[:20]}",
                overwrites=overwrites,
                category=category,                              # ← NUEVO
                reason=f"Canal para CTA #{cta_id}",
            )
            await APIClient.update_voice_channel(cta_id, str(vc.id))
            logger.info(
                "Canal de voz %s creado para CTA #%s (categoría=%s)",
                vc.id, cta_id, category.name if category else "raíz",
            )
        except Exception as exc:
            logger.error("Error creando canal de voz: %s", exc, exc_info=exc)

    def _pick_guild(self) -> discord.Guild | None:
        """Devuelve el guild de Linhir (por rol @Linhir o el único guild)."""
        for guild in self.bot.guilds:
            if guild.get_role(Config.LINHIR_ROLE_ID):
                return guild
        return self.bot.guilds[0] if self.bot.guilds else None

    # ================================================================== #
    #                 LISTENER: ENTRADA/SALIDA DE VOZ                    #
    # ================================================================== #

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return

        if before.channel == after.channel:
            return

        # SALIÓ (o se movió fuera del canal CTA)
        if before.channel:
            cta_id = await self._get_cta_id_by_voice_channel(before.channel.id)
            if cta_id:
                join_time = VOICE_SESSIONS.get(cta_id, {}).pop(member.id, None)
                if join_time:
                    seconds = int((datetime.now(timezone.utc) - join_time).total_seconds())
                    if seconds > 0:
                        await APIClient.add_voice_time(cta_id, str(member.id), seconds)

        # ENTRÓ (o se movió a un canal CTA)
        if after.channel:
            cta_id = await self._get_cta_id_by_voice_channel(after.channel.id)
            if cta_id:
                if cta_id not in VOICE_SESSIONS:
                    VOICE_SESSIONS[cta_id] = {}
                VOICE_SESSIONS[cta_id][member.id] = datetime.now(timezone.utc)

                try:
                    result, status = await APIClient.get_cta(cta_id)
                    if status == 200 and result.get("success"):
                        parts = result["cta"].get("participants", [])
                        is_enrolled = any(str(p.get("discord_user_id")) == str(member.id) and p.get("status") == "confirmed" for p in parts)
                        
                        if not is_enrolled:
                            msg = await after.channel.send(
                                f"👋 {member.mention}, veo que has entrado al canal. ¿Participas o estás de espectador?\n"
                                f"*(Usa `/cta unirse {cta_id}` si deseas participar)*"
                            )
                            await asyncio.sleep(30)
                            try: await msg.delete()
                            except Exception: pass
                except Exception as exc:
                    logger.error("Error consultando CTA en voz: %s", exc)

    async def _get_cta_id_by_voice_channel(self, voice_channel_id: int) -> int | None:
        try:
            result, status = await APIClient.get_ctas_with_voice()
            if status == 200 and result.get("success"):
                for cta in result["ctas"]:
                    if str(cta.get("discord_voice_channel_id")) == str(voice_channel_id):
                        return cta["id"]
        except Exception:
            pass
        return None
    

    # ================================================================== #
    #                    Grupo: /cta                                      #
    # ================================================================== #

    cta_group = app_commands.Group(name="cta", description="Gestión de CTAs del gremio")

    # ------------------- /cta crear -------------------

    @cta_group.command(name="crear", description="[Oficiales] Crea una nueva CTA")
    @app_commands.describe(
        composicion="Composición a usar", titulo="Título de la CTA", ubicacion="Ubicación",
        inicio="Inicio en DD/MM/YYYY HH:MM (UTC)", fin="Fin en DD/MM/YYYY HH:MM (UTC)",
        visibilidad="¿Para quién es la actividad?", loot_type="¿Cómo se repartirá el botín?",
        recordatorio="Minutos antes para DM (default 10)",
    )
    @app_commands.choices(
        visibilidad=[
            app_commands.Choice(name="Solo Linhir", value="linhir"),
            app_commands.Choice(name="Alianza HRL", value="alliance"),
        ],
        loot_type=[
            app_commands.Choice(name="Split con Impuesto (15%)", value="taxed_split"),
            app_commands.Choice(name="Full Split (Sin impuestos)", value="full_split"),
            app_commands.Choice(name="Pro-Gremio (Todo al banco)", value="pro_guild"),
        ]
    )
    @app_commands.autocomplete(composicion=composition_autocomplete)
    async def crear(
        self, interaction: discord.Interaction, composicion: str, titulo: str,
        ubicacion: str, inicio: str, fin: str,
        visibilidad: app_commands.Choice[str], loot_type: app_commands.Choice[str],
        recordatorio: int = 10,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            return await interaction.followup.send("❌ Solo oficiales.", ephemeral=True)

        start_iso, err = parse_datetime(inicio)
        if err: return await interaction.followup.send(f"❌ Inicio: {err}", ephemeral=True)
        end_iso, err = parse_datetime(fin)
        if err: return await interaction.followup.send(f"❌ Fin: {err}", ephemeral=True)

        loot_val = loot_type.value
        if visibilidad.value == "alliance": loot_val = "full_split"

        # 1. Crear evento de Discord
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        discord_event_id = None
        try:            
            event = await interaction.guild.create_scheduled_event(
                name=f"⚔️ {titulo}"[:100],
                description=f"CTA de {Config.GUILD_NAME}. Ubicación: {ubicacion}"[:1000],
                start_time=start_dt, end_time=end_dt,
                entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                location=ubicacion[:100], reason=f"CTA creada por {interaction.user}",
            )
            discord_event_id = str(event.id)
        except Exception as exc:
            logger.warning("No se pudo crear evento de Discord: %s", exc)

        payload = {
            "composition_id":        int(composicion),
            "title":                 titulo[:120],
            "location":              ubicacion[:120],
            "visibility":            visibilidad.value,
            "loot_type":             loot_val,
            "start_time":            start_iso,
            "end_time":              end_iso,
            "reminder_minutes":      max(1, min(120, recordatorio)),
            "discord_event_id":      discord_event_id,
            "created_by_discord_id": str(interaction.user.id),
            "created_by_name":       interaction.user.display_name,
            "announced":             False, # Se anunciará después
        }
        
        try:
            result, status = await APIClient.create_cta(payload)
            if status != 200 or not result.get("success"):
                return await interaction.followup.send(f"❌ {result.get('reason')}", ephemeral=True)
            
            cta_id = result["cta_id"]
            
            # 2. Preguntar cuándo anunciar
            embed = discord.Embed(
                title="📅 CTA Creada Exitosamente",
                description="¿Cuándo deseas que se envíe el anuncio (Ping) a Discord?",
                color=Config.EMBED_COLOR
            )
            ch_id = Config.CTA_ANNOUNCE_CHANNEL_ID if visibilidad.value == "linhir" else Config.CTA_CONTENT_CHANNEL_ID
            role_id = Config.LINHIR_ROLE_ID if visibilidad.value == "linhir" else Config.HRL_ROLE_ID
            
            view = AnnouncementTimeView(cta_id, ch_id, role_id, interaction.user.id, start_time=start_dt)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as exc:
            logger.error("Error creando CTA: %s", exc)
            await interaction.followup.send("❌ Error de conexión.", ephemeral=True)

    # ------------------- /cta unirse -------------------

    @cta_group.command(name="unirse", description="Únete a una CTA eligiendo un arma libre")
    @app_commands.describe(cta_id="ID de la CTA")
    async def unirse(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result, status = await APIClient.get_cta(cta_id)
            if status != 200 or not result.get("success"):
                return await interaction.followup.send("❌ CTA no encontrada.", ephemeral=True)
            
            cta = result["cta"]
            if cta.get("status") != "scheduled":
                return await interaction.followup.send("❌ CTA inactiva.", ephemeral=True)

            weapons = (cta.get("composition") or {}).get("weapons") or []
            occupancy = {str(p["composition_weapon_id"]): p["user_name"] for p in cta.get("participants") or [] if p.get("status") == "confirmed" and p.get("composition_weapon_id")}

            msg_id_raw = cta.get("discord_message_id")
            ch_id_raw = cta.get("discord_channel_id")
            msg_id = int(msg_id_raw) if msg_id_raw else 0
            ch_id = int(ch_id_raw) if ch_id_raw else interaction.channel_id

            view = WeaponSelectView(cta_id, weapons, occupancy, interaction.user.id, ch_id, msg_id)
            view.bot = interaction.client
            await interaction.followup.send("Selecciona tu arma:", view=view, ephemeral=True)
        except Exception as exc:
            logger.error("Error en /cta unirse: %s", exc)

    # ------------------- /cta ausente -------------------

    @cta_group.command(name="ausente", description="Marca tu ausencia en una CTA y libera tu arma")
    @app_commands.describe(cta_id="ID de la CTA")
    async def ausente(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result, status = await APIClient.cta_absent(cta_id, str(interaction.user.id))
            if status == 200 and result.get("success"):
                await interaction.followup.send(f"🚫 Marcado como ausente en la CTA **#{cta_id}**.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ {result.get('reason', 'Error.')}", ephemeral=True)
        except Exception as exc:
            logger.error("Error en /cta ausente: %s", exc)

    # ------------------- /cta cancelar -------------------

    @cta_group.command(name="cancelar", description="[Oficiales] Cancela una CTA, elimina evento y notifica")
    @app_commands.describe(cta_id="ID de la CTA", motivo="Motivo de cancelación", notificar="¿Enviar DM a inscritos?")
    async def cancelar(self, interaction: discord.Interaction, cta_id: int, motivo: str = "Cancelada por un oficial", notificar: bool = True):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await is_officer(interaction): return await interaction.followup.send("❌ Solo oficiales.", ephemeral=True)

        try:
            pre_result, pre_status = await APIClient.get_cta(cta_id)
            if pre_status != 200 or not pre_result.get("success"):
                return await interaction.followup.send("❌ CTA no encontrada.", ephemeral=True)
            
            cta_before = pre_result["cta"]
            if cta_before.get("status") != "scheduled":
                return await interaction.followup.send(f"⚠️ Ya está {cta_before.get('status')}.", ephemeral=True)

            participants = [p for p in (cta_before.get("participants") or []) if p.get("status") == "confirmed" and p.get("discord_user_id")]
            
            result, status = await APIClient.cta_cancel(cta_id)
            if status != 200 or not result.get("success"):
                return await interaction.followup.send("❌ Error al cancelar.", ephemeral=True)

            # Eliminar evento
            event_id = cta_before.get("discord_event_id")
            event_status = "sin evento"
            if event_id:
                try:
                    event = await interaction.guild.fetch_scheduled_event(int(event_id))
                    await event.delete(reason="CTA cancelada")
                    event_status = "evento eliminado"
                except Exception as exc:
                    event_status = "error al eliminar evento"
                    logger.warning("Evento no borrado: %s", exc)

            # Actualizar embed público
            cta_res, _ = await APIClient.get_cta(cta_id)
            if cta_res and cta_res.get("success"):
                cta = cta_res["cta"]
                msg_id = cta.get("discord_message_id")
                ch_id = cta.get("discord_channel_id")
                if msg_id and ch_id:
                    channel = self.bot.get_channel(int(ch_id))
                    if channel:
                        msg = await channel.fetch_message(int(msg_id))
                        view = CtaAnnouncementView(cta_id)
                        for c in view.children: c.disabled = True
                        await msg.edit(embed=build_cta_embed(cta), view=view)

            # Notificar
            notified = 0
            if notificar and participants:
                body = f"⚠️ **CTA cancelada**\n\n**{cta_before.get('title')}**\nMotivo: {motivo[:200]}"
                for p in participants:
                    try:
                        user = await self.bot.fetch_user(int(p["discord_user_id"]))
                        await user.send(body)
                        notified += 1
                    except Exception: pass

            await interaction.followup.send(
                f"✅ CTA #{cta_id} cancelada.\n📅 {event_status}\n📩 Notificados: {notified}/{len(participants)}",
                ephemeral=True
            )
        except Exception as exc:
            logger.error("Error en /cta cancelar: %s", exc)

    # ------------------- /cta pagar -------------------

    @cta_group.command(name="pagar", description="[Oficiales] Paga una CTA y distribuye el botín")
    @app_commands.describe(
        cta_id="ID de la CTA", silver="Silver líquido", items="Valor estimado de items",
        loot_type="Tipo de reparto final"
    )
    @app_commands.choices(loot_type=[
        app_commands.Choice(name="Split con Impuesto (15%)", value="taxed_split"),
        app_commands.Choice(name="Full Split (Sin impuestos)", value="full_split"),
        app_commands.Choice(name="Pro-Gremio (Todo al banco)", value="pro_guild"),
    ])
    async def pagar(self, interaction: discord.Interaction, cta_id: int, silver: int, items: int = 0, loot_type: app_commands.Choice[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await is_officer(interaction): return await interaction.followup.send("❌ Solo oficiales.", ephemeral=True)
        if silver < 0 or items < 0: return await interaction.followup.send("❌ Montos negativos.", ephemeral=True)

        try:
            result, status = await APIClient.get_cta(cta_id)
            if status != 200: return await interaction.followup.send("❌ CTA no encontrada.", ephemeral=True)
            cta = result["cta"]
            if cta.get("paid"): return await interaction.followup.send("⚠️ Ya está pagada.", ephemeral=True)
            
            loot_val = loot_type.value if loot_type else cta.get("loot_type", "taxed_split")
            if cta.get("visibility") == "alliance": loot_val = "full_split"
            
            if loot_val == "pro_guild":
                payload = {
                    "cta_id": cta_id, "name": cta.get("title"), "start_time": cta["start_time"], "end_time": cta["end_time"],
                    "liquid_silver": silver, "items_value": items, "pro_guild": True, "loot_type": "pro_guild",
                    "participants": [], "registered_by_discord_id": str(interaction.user.id), "registered_by_name": interaction.user.display_name
                }
                r, s = await APIClient.create_activity(payload)
                if s == 200 and r.get("success"):
                    embed = discord.Embed(title="✅ Pago Pro-Gremio registrado", description=f"Total: `{format_number(silver+items)}`", color=Config.EMBED_COLOR)
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send(f"❌ {r.get('reason')}")
                return

            members_data, m_status = await APIClient.get_registered_members()
            personajes = [{"name": p["name"], "discord_user_id": str(p["discord_user_id"])} for p in members_data.get("guild_registered", [])]
            personajes_by_uid = {p["discord_user_id"]: p["name"] for p in personajes}

            confirmed = [p for p in cta.get("participants", []) if p.get("status") == "confirmed"]
            preseed = [{"discord_user_id": str(p["discord_user_id"]), "name": personajes_by_uid[str(p["discord_user_id"])], "percent": 100} for p in confirmed if str(p.get("discord_user_id")) in personajes_by_uid]

            if not preseed: return await interaction.followup.send("⚠️ Ningún participante está vinculado.", ephemeral=True)

            from cogs.activities import ActivityBuilderView
            base_payload = {
                "cta_id": cta_id, "name": cta.get("title"), "start_time": cta["start_time"], "end_time": cta["end_time"],
                "liquid_silver": silver, "items_value": items, "pro_guild": False, "loot_type": loot_val,
                "registered_by_discord_id": str(interaction.user.id), "registered_by_name": interaction.user.display_name
            }
            
            view = ActivityBuilderView(interaction.user.id, base_payload, personajes, preseed)
            panel = await interaction.channel.send(
                content=f"💰 **Pago de CTA #{cta_id} ({loot_val})**\nParticipantes precargados: {len(preseed)}",
                embed=view.build_embed(), view=view
            )
            view.message = panel
            await interaction.followup.send("✅ Panel abierto.", ephemeral=True)

        except Exception as exc:
            logger.error("Error en /cta pagar: %s", exc)

    # ------------------- /cta ver -------------------

    @cta_group.command(name="ver", description="Muestra el detalle completo de una CTA")
    @app_commands.describe(cta_id="ID de la CTA")
    async def ver(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(thinking=True)
        try:
            result, status = await APIClient.get_cta(cta_id)
            if status == 200 and result.get("success"):
                embed = build_cta_embed(result["cta"])
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ CTA no encontrada.", ephemeral=True)
        except Exception as exc:
            logger.error("Error en /cta ver: %s", exc)

    # ------------------- /ctas -------------------

    @app_commands.command(name="ctas", description="Lista CTAs por estado")
    @app_commands.describe(filtro="Qué CTAs listar", limite="Cuántas mostrar (5-25)")
    @app_commands.choices(filtro=[
        app_commands.Choice(name="⚔️  Programadas",  value="scheduled"),
        app_commands.Choice(name="💰  Por pagar",    value="por_pagar"),
        app_commands.Choice(name="✅  Pagadas",      value="pagadas"),
        app_commands.Choice(name="❌  Canceladas",   value="cancelled"),
        app_commands.Choice(name="📜  Todas",        value="all"),
    ])
    async def listar(self, interaction: discord.Interaction, filtro: app_commands.Choice[str] | None = None, limite: int = 15):
        await interaction.response.defer(thinking=True)
        limite = max(5, min(25, limite))
        filtro_val = filtro.value if filtro else "scheduled"

        try:
            if filtro_val == "scheduled":
                result, status = await APIClient.get_ctas(status="scheduled", paid=False, limit=limite)
                titulo, subtitulo = "⚔️ CTAs programadas", "Próximas actividades sin pagar"
            elif filtro_val == "por_pagar":
                result, status = await APIClient.get_pending_payment_ctas()
                titulo, subtitulo = "💰 CTAs por pagar", "Actividades pasadas pendientes de pago"
                if result.get("ctas"): result["ctas"] = result["ctas"][:limite]
            elif filtro_val == "pagadas":
                result, status = await APIClient.get_ctas(paid=True, limit=limite)
                titulo, subtitulo = "✅ CTAs pagadas", "Últimas actividades liquidadas"
            elif filtro_val == "cancelled":
                result, status = await APIClient.get_ctas(status="cancelled", limit=limite)
                titulo, subtitulo = "❌ CTAs canceladas", "Actividades canceladas"
            else:
                result, status = await APIClient.get_ctas(limit=limite)
                titulo, subtitulo = "📜 Historial de CTAs", "Todas las actividades"

            if status != 200 or not result.get("success"):
                return await interaction.followup.send("❌ Error al consultar.", ephemeral=True)

            ctas = result.get("ctas", [])
            if not ctas:
                return await interaction.followup.send(f"📭 No hay CTAs en **{filtro_val}**.")

            embed = discord.Embed(title=titulo, description=f"*{subtitulo}* · {len(ctas)} resultado(s)", color=Config.EMBED_COLOR)

            for cta in ctas[:15]:
                start_iso = cta.get("start_time")
                time_str = start_iso
                try:
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    time_str = f"<t:{int(start_dt.timestamp())}:R>"
                except: pass

                badges = []
                status_txt = cta.get("status", "?")
                if status_txt == "scheduled": badges.append("🕐 programada")
                elif status_txt == "cancelled": badges.append("❌ cancelada")
                elif status_txt == "completed": badges.append("✅ completada")
                if cta.get("paid"): badges.append("💵 pagada")

                embed.add_field(
                    name=f"#{cta.get('id')} · {cta.get('title', '—')[:70]}",
                    value=f"📍 {cta.get('location', '—')}\n🎭 {(cta.get('composition') or {}).get('name', '—')} · 👥 {cta.get('confirmed_count', 0)}\n⏰ {time_str}\n{' · '.join(badges)}",
                    inline=False,
                )

            embed.set_footer(text=f"Filtro: {filtro_val}")
            await interaction.followup.send(embed=embed)

        except Exception as exc:
            logger.error("Error en /ctas: %s", exc)

    # ================================================================== #
    #                          Error handler                             #
    # ================================================================== #

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error("Error en CtasCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
            else: await interaction.response.send_message(msg, ephemeral=True)
        except Exception: pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CtasCog(bot))