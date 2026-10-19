"""Cog de CTAs: creación, inscripción, ausencias, pagos y recordatorios.

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
"""

import logging
import re
from datetime import datetime, timezone

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

    color_map = {
        "scheduled": Config.EMBED_COLOR,
        "cancelled": 0xFF5252,
        "completed": 0x5CCB45,
    }
    color = color_map.get(status, Config.EMBED_COLOR)

    embed = discord.Embed(
        title=f"⚔️ {title}",
        description=(
            f"📍 **{location}**\n"
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
            embed.add_field(
                name="⏰ Inicio",
                value=f"<t:{ts}:F>\n(<t:{ts}:R>)",
                inline=True,
            )
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
        if paid:
            estado_txt += " · 💵 Pagada"
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

    async def cog_unload(self):
        self.reminder_loop.cancel()

    async def _verify_cta_publications(self):
        """
        Verifica cuántas CTAs activas tienen IDs de Discord guardados.

        NO registra vistas persistentes: los botones se manejan con
        `on_interaction`. Solo hace un log de diagnóstico.
        """
        try:
            result, status = await APIClient.get_active_ctas()
        except (AuthenticationError, AuthorizationError):
            return

        if status != 200 or not result.get("success"):
            return

        ctas = result.get("ctas", [])
        with_ids = sum(
            1 for c in ctas
            if c.get("discord_message_id") and c.get("discord_channel_id")
        )
        logger.info(
            "CTAs activas: %d · con IDs de Discord: %d/%d "
            "(los botones se manejan con on_interaction)",
            len(ctas), with_ids, len(ctas),
        )

    # ================================================================== #
    #                 LISTENER GLOBAL: BOTONES DE CTA                     #
    # ================================================================== #

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """
        Captura TODOS los botones con custom_id de CTA (web + bot).

        Patrón aceptado:
          linhir:cta:join:{id}     (formato actual, web y bot)
          linhir:cta:absent:{id}
          linhir:cta:refresh:{id}
          cta:join:{id}            (formato legacy, retrocompatible)
          cta:absent:{id}
          cta:refresh:{id}
        """
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = (
            interaction.data.get("custom_id", "")
            if interaction.data else ""
        )

        logger.info(
            "CTA interaction: custom_id=%r user=%s",
            custom_id, interaction.user,
        )

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

        logger.debug("CTA interaction ignorada (no match): %r", custom_id)

    # ================================================================== #
    #                    HANDLERS DE BOTONES                              #
    # ================================================================== #

    async def _handle_join_btn(self, interaction: discord.Interaction, cta_id: int):
        """Handler del botón 'Unirse': defer inmediato → get_cta → selector."""
        # 1. DEFER PRIMERO (antes de cualquier llamada a la API)
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 2. Consultar CTA
        try:
            result, status = await APIClient.get_cta(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except Exception as exc:
            logger.exception("join_btn: error get_cta %s", cta_id)
            await interaction.followup.send(
                f"❌ No se pudo consultar la CTA: {exc}", ephemeral=True
            )
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo cargar la CTA.", ephemeral=True
            )
            return

        cta = result["cta"]
        if cta.get("status") != "scheduled":
            await interaction.followup.send(
                "❌ Esta CTA ya no está activa.", ephemeral=True
            )
            return

        composition = cta.get("composition") or {}
        weapons = composition.get("weapons") or []

        occupancy: dict[str, str] = {}
        for p in cta.get("participants") or []:
            if p.get("status") == "confirmed" and p.get("composition_weapon_id"):
                occupancy[str(p["composition_weapon_id"])] = p["user_name"]

        if not weapons:
            await interaction.followup.send(
                "⚠️ Esta CTA no tiene composición con armas.", ephemeral=True
            )
            return

        # 3. Selector de arma efímero
        view = WeaponSelectView(
            cta_id=cta_id,
            weapons=weapons,
            occupancy=occupancy,
            author_id=interaction.user.id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id if interaction.message else 0,
        )
        view.bot = interaction.client

        await interaction.followup.send(
            "Selecciona el arma con la que participarás:",
            view=view,
            ephemeral=True,
        )

    async def _handle_absent_btn(self, interaction: discord.Interaction, cta_id: int):
        """Handler del botón 'Ausente': defer primero, luego API."""
        await interaction.response.defer(ephemeral=True)

        try:
            result, status = await APIClient.cta_absent(
                cta_id, str(interaction.user.id)
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except Exception as exc:
            logger.exception("absent_btn: error %s", cta_id)
            await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo registrar tu ausencia.')}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "🚫 Marcado como ausente. Se ha liberado tu arma (si tenías una).",
            ephemeral=True,
        )

        await self._refresh_public_cta_message(interaction, cta_id)

    async def _handle_refresh_btn(self, interaction: discord.Interaction, cta_id: int):
        """Handler del botón '🔄': defer primero, luego refrescar mensaje."""
        await interaction.response.defer()
        await self._refresh_public_cta_message(interaction, cta_id)

    async def _refresh_public_cta_message(
        self, interaction: discord.Interaction, cta_id: int
    ):
        """Recarga la CTA y edita el mensaje público original (si se puede)."""
        if not interaction.message:
            return

        try:
            result, status = await APIClient.get_cta(cta_id)
            if status != 200 or not result.get("success"):
                return

            embed = build_cta_embed(result["cta"])
            new_view = CtaAnnouncementView(cta_id=cta_id)

            await interaction.message.edit(embed=embed, view=new_view)
        except Exception as exc:
            logger.warning("No se pudo refrescar CTA #%s: %s", cta_id, exc)

    # ================================================================== #
    #                     Tarea: recordatorios por DM                     #
    # ================================================================== #

    @tasks.loop(seconds=60)
    async def reminder_loop(self):
        try:
            await self._process_reminders()
        except Exception as exc:
            logger.exception("Error en reminder_loop: %s", exc)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()
        self.reminder_loop.change_interval(
            seconds=Config.CTA_REMINDER_POLL_SECONDS
        )
        logger.info(
            "Poller de recordatorios de CTAs iniciado (cada %ds)",
            Config.CTA_REMINDER_POLL_SECONDS,
        )

    async def _process_reminders(self):
        try:
            result, status = await APIClient.get_pending_reminders()
        except (AuthenticationError, AuthorizationError) as exc:
            logger.warning("Recordatorios: auth error %s", exc)
            return

        if status != 200 or not result.get("success"):
            return

        ctas = result.get("ctas", [])
        if not ctas:
            return

        logger.info("Recordatorios pendientes: %d", len(ctas))

        for cta in ctas:
            cta_id = cta["id"]
            title = cta.get("title", "—")
            location = cta.get("location", "—")
            start_iso = cta.get("start_time")
            participants = cta.get("confirmed_participants") or []

            if not participants:
                await APIClient.mark_reminder_sent(cta_id)
                continue

            start_fmt = start_iso
            try:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                ts = int(start_dt.timestamp())
                start_fmt = f"<t:{ts}:R>"
            except (ValueError, TypeError, AttributeError):
                pass

            body = (
                f"🔔 **Recordatorio de CTA**\n\n"
                f"**{title}**\n"
                f"📍 {location}\n"
                f"⏰ Inicia {start_fmt}\n\n"
                f"¡No olvides asistir!"
            )

            sent = 0
            failed = 0
            for p in participants:
                uid = p.get("discord_user_id")
                if not uid:
                    continue
                try:
                    user = await self.bot.fetch_user(int(uid))
                    await user.send(body)
                    sent += 1
                except (discord.Forbidden, discord.NotFound):
                    failed += 1
                except discord.HTTPException as exc:
                    failed += 1
                    logger.warning("DM fallo para %s: %s", uid, exc)

            await APIClient.mark_reminder_sent(cta_id)

            logger.info(
                "Recordatorio CTA #%s: %d enviados, %d fallidos",
                cta_id, sent, failed,
            )

    # ================================================================== #
    #                    Grupo: /cta                                      #
    # ================================================================== #

    cta_group = app_commands.Group(name="cta", description="Gestión de CTAs del gremio")

    # ------------------- /cta crear -------------------

    @cta_group.command(
        name="crear",
        description="[Oficiales] Crea una nueva CTA con anuncio y evento de Discord",
    )
    @app_commands.describe(
        composicion="Composición a usar (autocompletado)",
        titulo="Título de la CTA",
        ubicacion="Ubicación (ej: 'Black Zone Portal Rojo')",
        inicio="Inicio en DD/MM/YYYY HH:MM (UTC)",
        fin="Fin en DD/MM/YYYY HH:MM (UTC)",
        recordatorio="Minutos antes para DM de recordatorio (default 10)",
        mencionar="Rol a mencionar en el anuncio (opcional)",
    )
    @app_commands.autocomplete(composicion=composition_autocomplete)
    async def crear(
        self,
        interaction: discord.Interaction,
        composicion: str,
        titulo: str,
        ubicacion: str,
        inicio: str,
        fin: str,
        recordatorio: int = 10,
        mencionar: discord.Role | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden crear CTAs.", ephemeral=True
            )
            return

        if not Config.CTA_ANNOUNCE_CHANNEL_ID:
            await interaction.followup.send(
                "❌ Falta configurar `CTA_ANNOUNCE_CHANNEL_ID` en el `.env`.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(Config.CTA_ANNOUNCE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ El canal de anuncios de CTAs no es válido.", ephemeral=True
            )
            return

        start_iso, err = parse_datetime(inicio)
        if err:
            await interaction.followup.send(f"❌ Inicio: {err}", ephemeral=True)
            return

        end_iso, err = parse_datetime(fin)
        if err:
            await interaction.followup.send(f"❌ Fin: {err}", ephemeral=True)
            return

        if end_iso <= start_iso:
            await interaction.followup.send(
                "❌ La hora de fin debe ser posterior al inicio.", ephemeral=True
            )
            return

        try:
            composition_id = int(composicion)
        except (ValueError, TypeError):
            await interaction.followup.send(
                "❌ Composición inválida. Usa el autocompletado.", ephemeral=True
            )
            return

        recordatorio = max(1, min(120, recordatorio))

        # 1. Crear evento de Discord
        discord_event_id = None
        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_dt = datetime.fromisoformat(end_iso)
            event = await interaction.guild.create_scheduled_event(
                name=f"⚔️ {titulo}"[:100],
                description=f"CTA de {Config.GUILD_NAME}. Ubicación: {ubicacion}"[:1000],
                start_time=start_dt,
                end_time=end_dt,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=ubicacion[:100],
                reason=f"CTA creada por {interaction.user}",
            )
            discord_event_id = str(event.id)
        except discord.Forbidden:
            logger.warning("Sin permiso Manage Events; se omite evento de Discord")
        except Exception as exc:
            logger.exception("Error creando evento: %s", exc)

        # 2. Enviar mensaje placeholder SIN vista
        placeholder_embed = discord.Embed(
            title=f"⚔️ {titulo}",
            description=f"📍 **{ubicacion}**\n⏳ Publicando...",
            color=Config.EMBED_COLOR,
        )

        content = mencionar.mention if mencionar else None
        allowed = (
            discord.AllowedMentions(roles=[mencionar])
            if mencionar
            else discord.AllowedMentions.none()
        )

        msg = await channel.send(
            content=content,
            embed=placeholder_embed,
            allowed_mentions=allowed,
        )

        # 3. Registrar en el backend
        payload = {
            "composition_id":        composition_id,
            "title":                 titulo[:120],
            "location":              ubicacion[:120],
            "start_time":            start_iso,
            "end_time":              end_iso,
            "reminder_minutes":      recordatorio,
            "discord_message_id":    str(msg.id),
            "discord_channel_id":    str(channel.id),
            "discord_event_id":      discord_event_id,
            "discord_role_id":       str(mencionar.id) if mencionar else None,
            "created_by_discord_id": str(interaction.user.id),
            "created_by_name":       interaction.user.display_name,
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        try:
            result, status = await APIClient.create_cta(payload)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            try:
                await msg.delete()
            except Exception:
                pass
            if discord_event_id:
                try:
                    ev = await interaction.guild.fetch_scheduled_event(int(discord_event_id))
                    await ev.delete(reason="Rollback por error al registrar CTA")
                except Exception:
                    pass
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo registrar la CTA.')}",
                ephemeral=True,
            )
            try:
                await msg.delete()
            except Exception:
                pass
            if discord_event_id:
                try:
                    ev = await interaction.guild.fetch_scheduled_event(int(discord_event_id))
                    await ev.delete(reason="Rollback por error al registrar CTA")
                except Exception:
                    pass
            return

        cta_id = result["cta_id"]

        # 4. Actualizar mensaje con embed real + vista visual
        try:
            cta_result, cta_status = await APIClient.get_cta(cta_id)
            if cta_status == 200 and cta_result.get("success"):
                real_embed = build_cta_embed(cta_result["cta"])
                real_view = CtaAnnouncementView(cta_id=cta_id)
                await msg.edit(embed=real_embed, view=real_view)
        except Exception as exc:
            logger.warning("No se pudo refrescar el mensaje con datos reales: %s", exc)

        await interaction.followup.send(
            f"✅ CTA **#{cta_id}** creada y publicada en {channel.mention}.",
            ephemeral=True,
        )
        logger.info(
            "CTA #%s creada por %s (composición=%s, evento=%s)",
            cta_id, interaction.user, composition_id, discord_event_id,
        )

    # ------------------- /cta unirse -------------------

    @cta_group.command(
        name="unirse",
        description="Únete a una CTA eligiendo un arma libre",
    )
    @app_commands.describe(cta_id="ID de la CTA (ver /ctas)")
    async def unirse(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_cta(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'CTA no encontrada.')}", ephemeral=True
            )
            return

        cta = result["cta"]
        if cta.get("status") != "scheduled":
            await interaction.followup.send(
                "❌ Esta CTA ya no está activa.", ephemeral=True
            )
            return

        composition = cta.get("composition") or {}
        weapons = composition.get("weapons") or []
        occupancy = {}
        for p in cta.get("participants") or []:
            if p.get("status") == "confirmed" and p.get("composition_weapon_id"):
                occupancy[str(p["composition_weapon_id"])] = p["user_name"]

        if not weapons:
            await interaction.followup.send(
                "⚠️ Esta CTA no tiene composición con armas.", ephemeral=True
            )
            return

        msg_id_raw = cta.get("discord_message_id")
        ch_id_raw = cta.get("discord_channel_id")
        try:
            msg_id = int(msg_id_raw) if msg_id_raw else 0
            ch_id = int(ch_id_raw) if ch_id_raw else interaction.channel_id
        except (ValueError, TypeError):
            msg_id = 0
            ch_id = interaction.channel_id

        view = WeaponSelectView(
            cta_id=cta_id,
            weapons=weapons,
            occupancy=occupancy,
            author_id=interaction.user.id,
            channel_id=ch_id,
            message_id=msg_id,
        )
        view.bot = interaction.client

        await interaction.followup.send(
            "Selecciona el arma con la que participarás:",
            view=view,
            ephemeral=True,
        )

    # ------------------- /cta ausente -------------------

    @cta_group.command(
        name="ausente",
        description="Marca tu ausencia en una CTA y libera tu arma",
    )
    @app_commands.describe(cta_id="ID de la CTA")
    async def ausente(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.cta_absent(
                cta_id, str(interaction.user.id)
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo registrar tu ausencia.')}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🚫 Marcado como ausente en la CTA **#{cta_id}**.", ephemeral=True
        )

    # ------------------- /cta cancelar -------------------

    @cta_group.command(
        name="cancelar",
        description="[Oficiales] Cancela una CTA, elimina su evento de Discord y notifica a los inscritos",
    )
    @app_commands.describe(
        cta_id="ID de la CTA a cancelar",
        motivo="Motivo de la cancelación (se enviará a los inscritos)",
        notificar="Enviar DM a los inscritos (default: sí)",
    )
    async def cancelar(
        self,
        interaction: discord.Interaction,
        cta_id: int,
        motivo: str = "Cancelada por un oficial",
        notificar: bool = True,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden cancelar CTAs.", ephemeral=True
            )
            return

        try:
            pre_result, pre_status = await APIClient.get_cta(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if pre_status != 200 or not pre_result.get("success"):
            await interaction.followup.send(
                f"❌ {pre_result.get('reason', 'CTA no encontrada.')}", ephemeral=True
            )
            return

        cta_before = pre_result["cta"]

        if cta_before.get("status") != "scheduled":
            await interaction.followup.send(
                f"⚠️ Esta CTA ya está **{cta_before.get('status')}**. "
                f"No hay nada que cancelar.",
                ephemeral=True,
            )
            return

        participants = [
            p for p in (cta_before.get("participants") or [])
            if p.get("status") == "confirmed" and p.get("discord_user_id")
        ]
        cta_title = cta_before.get("title", "—")

        try:
            result, status = await APIClient.cta_cancel(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo cancelar.')}", ephemeral=True
            )
            return

        # Eliminar evento de Discord
        event_id = cta_before.get("discord_event_id")
        event_status = "sin evento vinculado"

        if event_id:
            try:
                event = await interaction.guild.fetch_scheduled_event(int(event_id))
                event_name = event.name
                await event.delete(reason=f"CTA cancelada por {interaction.user}")
                event_status = f"evento `{event_name}` eliminado"
                logger.info(
                    "Evento de Discord %s eliminado para CTA #%s", event_id, cta_id
                )
            except discord.NotFound:
                event_status = "evento ya no existía en Discord"
                logger.warning(
                    "Evento %s no encontrado para CTA #%s (quizá ya fue eliminado)",
                    event_id, cta_id,
                )
            except discord.Forbidden:
                event_status = "⚠️ sin permisos para eliminar el evento"
                logger.error(
                    "Sin permisos para eliminar evento %s (CTA #%s)", event_id, cta_id
                )
            except discord.HTTPException as exc:
                event_status = f"⚠️ error HTTP ({exc.status}) al eliminar el evento"
                logger.exception("Error HTTP eliminando evento %s: %s", event_id, exc)

        # Actualizar mensaje público
        try:
            cta_result, cta_status = await APIClient.get_cta(cta_id)
            if cta_status == 200 and cta_result.get("success"):
                cta = cta_result["cta"]
                msg_id = cta.get("discord_message_id")
                ch_id = cta.get("discord_channel_id")
                if msg_id and ch_id:
                    channel = self.bot.get_channel(int(ch_id))
                    if channel:
                        msg = await channel.fetch_message(int(msg_id))
                        new_embed = build_cta_embed(cta)

                        view = CtaAnnouncementView(cta_id=cta_id)
                        for c in view.children:
                            c.disabled = True

                        await msg.edit(embed=new_embed, view=view)
        except Exception as exc:
            logger.warning("No se pudo actualizar mensaje de CTA cancelada: %s", exc)

        # Notificar por DM
        notified = 0
        failed_notify = 0

        if notificar and participants:
            dm_body = (
                f"⚠️ **CTA cancelada**\n\n"
                f"**{cta_title}**\n"
                f"Motivo: {motivo[:200]}\n\n"
                f"Disculpa las molestias."
            )
            for p in participants:
                try:
                    user = await self.bot.fetch_user(int(p["discord_user_id"]))
                    await user.send(dm_body)
                    notified += 1
                except (discord.Forbidden, discord.NotFound):
                    failed_notify += 1
                except discord.HTTPException as exc:
                    failed_notify += 1
                    logger.warning("DM fallo al notificar cancelación: %s", exc)

        summary_lines = [
            f"✅ CTA **#{cta_id}** cancelada.",
            f"📅 Evento: {event_status}",
        ]
        if notificar and participants:
            summary_lines.append(
                f"📩 Notificados: {notified}/{len(participants)}"
                + (f" ({failed_notify} fallidos)" if failed_notify else "")
            )
        elif notificar and not participants:
            summary_lines.append("📩 Sin inscritos a notificar")

        await interaction.followup.send("\n".join(summary_lines), ephemeral=True)

        logger.info(
            "CTA #%s cancelada por %s | evento=%s | notificados=%d/%d",
            cta_id, interaction.user, event_status, notified, len(participants),
        )

    # ------------------- /cta pagar -------------------

    @cta_group.command(
        name="pagar",
        description="[Oficiales] Paga una CTA y distribuye el botín entre sus participantes",
    )
    @app_commands.describe(
        cta_id="ID de la CTA a pagar (ver /ctas)",
        silver="Silver líquido recolectado",
        items="Valor estimado de los items (opcional)",
        pro_gremio="Si es True, TODO el botín va al gremio (sin repartir)",
    )
    async def pagar(
        self,
        interaction: discord.Interaction,
        cta_id: int,
        silver: int,
        items: int = 0,
        pro_gremio: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden pagar CTAs.", ephemeral=True
            )
            return

        if silver < 0 or items < 0:
            await interaction.followup.send(
                "❌ Los montos no pueden ser negativos.", ephemeral=True
            )
            return

        # ------------------------------------------------------------------
        # 1. Cargar la CTA
        # ------------------------------------------------------------------
        try:
            result, status = await APIClient.get_cta(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'CTA no encontrada.')}", ephemeral=True
            )
            return

        cta = result["cta"]

        if cta.get("paid"):
            await interaction.followup.send(
                f"⚠️ La CTA **#{cta_id}** ya fue pagada.", ephemeral=True
            )
            return
        if cta.get("status") == "cancelled":
            await interaction.followup.send(
                f"⚠️ La CTA **#{cta_id}** está cancelada. No se puede pagar.",
                ephemeral=True,
            )
            return

        # ------------------------------------------------------------------
        # 2. Modo pro-gremio
        # ------------------------------------------------------------------
        if pro_gremio:
            payload = {
                "cta_id":                    cta_id,
                "name":                      cta.get("title", f"CTA #{cta_id}"),
                "start_time":                cta["start_time"],
                "end_time":                  cta["end_time"],
                "liquid_silver":             int(silver),
                "items_value":               int(items),
                "pro_guild":                 True,
                "participants":              [],
                "registered_by_discord_id":  str(interaction.user.id),
                "registered_by_name":        interaction.user.display_name,
            }

            try:
                r, s = await APIClient.create_activity(payload)
            except (AuthenticationError, AuthorizationError) as exc:
                await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
                return

            if s != 200 or not r.get("success"):
                await interaction.followup.send(
                    f"❌ {r.get('reason', 'No se pudo registrar.')}",
                    ephemeral=True,
                )
                return

            total_bruto = int(silver) + int(items)
            embed = discord.Embed(
                title="✅ CTA pagada como pro-gremio",
                description=(
                    f"**{cta.get('title', f'CTA #{cta_id}')}**\n\n"
                    f"Silver: `{format_number(silver)}`\n"
                    f"Items: `{format_number(items)}`\n"
                    f"**Total al gremio:** `{format_number(total_bruto)}` plata"
                ),
                color=Config.EMBED_COLOR,
            )
            embed.set_footer(text=f"CTA #{cta_id} marcada como pagada")
            await interaction.followup.send(embed=embed)
            return

        # ------------------------------------------------------------------
        # 3. Modo normal → precargar participantes
        # ------------------------------------------------------------------
        try:
            members_data, members_status = await APIClient.get_registered_members()
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if members_status != 200 or not members_data.get("success"):
            await interaction.followup.send(
                "❌ No se pudo cargar la lista de miembros.", ephemeral=True
            )
            return

        guild_registered = members_data.get("guild_registered", [])
        personajes = [
            {
                "name":            p["name"],
                "discord_user_id": str(p["discord_user_id"]),
            }
            for p in guild_registered
        ]
        personajes_by_uid = {p["discord_user_id"]: p["name"] for p in personajes}

        confirmed = [
            p for p in (cta.get("participants") or [])
            if p.get("status") == "confirmed"
        ]

        if not confirmed:
            await interaction.followup.send(
                f"⚠️ La CTA **#{cta_id}** no tiene participantes confirmados.",
                ephemeral=True,
            )
            return

        preseed = []
        sin_personaje = []
        for p in confirmed:
            uid = str(p.get("discord_user_id", ""))
            if uid in personajes_by_uid:
                preseed.append({
                    "discord_user_id": uid,
                    "name":            personajes_by_uid[uid],
                    "percent":         100,
                })
            else:
                sin_personaje.append(p.get("user_name", "—"))

        if not preseed:
            await interaction.followup.send(
                "⚠️ Ninguno de los participantes de la CTA tiene personaje "
                "vinculado en el gremio. No se puede distribuir.",
                ephemeral=True,
            )
            return

        # Import local para evitar circular imports (activities ↔ ctas)
        from cogs.activities import ActivityBuilderView

        base_payload = {
            "cta_id":                    cta_id,
            "name":                      cta.get("title", f"CTA #{cta_id}"),
            "start_time":                cta["start_time"],
            "end_time":                  cta["end_time"],
            "liquid_silver":             int(silver),
            "items_value":               int(items),
            "pro_guild":                 False,
            "registered_by_discord_id":  str(interaction.user.id),
            "registered_by_name":        interaction.user.display_name,
        }

        view = ActivityBuilderView(
            author_id=interaction.user.id,
            base_payload=base_payload,
            personajes=personajes,
            preseed_participants=preseed,
        )

        panel_msg = await interaction.channel.send(
            content=(
                f"💰 **Pago de CTA #{cta_id}** · {cta.get('title', '')}\n"
                f"Participantes precargados: **{len(preseed)}**\n"
                f"*(Revisa la lista, quita a quien no asistió, ajusta % y confirma)*"
            ),
            embed=view.build_embed(),
            view=view,
        )
        view.message = panel_msg

        if sin_personaje:
            await interaction.followup.send(
                f"⚠️ **Sin personaje vinculado** (no se incluyen):\n"
                + "\n".join(f"• {n}" for n in sin_personaje[:10]),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Panel de pago abierto para la CTA **#{cta_id}**.",
                ephemeral=True,
            )

    # ------------------- /cta ver -------------------

    @cta_group.command(
        name="ver",
        description="Muestra el detalle completo de una CTA",
    )
    @app_commands.describe(cta_id="ID de la CTA")
    async def ver(self, interaction: discord.Interaction, cta_id: int):
        await interaction.response.defer(thinking=True)

        try:
            result, status = await APIClient.get_cta(cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'CTA no encontrada.')}", ephemeral=True
            )
            return

        embed = build_cta_embed(result["cta"])
        await interaction.followup.send(embed=embed)

    # ------------------- /ctas (lista con filtros) -------------------

    @app_commands.command(
        name="ctas",
        description="Lista CTAs por estado (programadas, por pagar, pagadas, canceladas)",
    )
    @app_commands.describe(
        filtro="Qué CTAs listar (default: programadas)",
        limite="Cuántas mostrar (5-25)",
    )
    @app_commands.choices(filtro=[
        app_commands.Choice(name="⚔️  Programadas",  value="scheduled"),
        app_commands.Choice(name="💰  Por pagar",    value="por_pagar"),
        app_commands.Choice(name="✅  Pagadas",      value="pagadas"),
        app_commands.Choice(name="❌  Canceladas",   value="cancelled"),
        app_commands.Choice(name="📜  Todas",        value="all"),
    ])
    async def listar(
        self,
        interaction: discord.Interaction,
        filtro: app_commands.Choice[str] | None = None,
        limite: int = 15,
    ):
        await interaction.response.defer(thinking=True)

        limite = max(5, min(25, limite))
        filtro_val = filtro.value if filtro else "scheduled"

        try:
            if filtro_val == "scheduled":
                result, status = await APIClient.get_ctas(
                    status="scheduled", paid=False, limit=limite
                )
                titulo = "⚔️ CTAs programadas"
                subtitulo = "Próximas actividades sin pagar"

            elif filtro_val == "por_pagar":
                result, status = await APIClient.get_pending_payment_ctas()
                titulo = "💰 CTAs por pagar"
                subtitulo = "Actividades pasadas pendientes de pago"
                if result.get("ctas"):
                    result["ctas"] = result["ctas"][:limite]

            elif filtro_val == "pagadas":
                result, status = await APIClient.get_ctas(
                    paid=True, limit=limite
                )
                titulo = "✅ CTAs pagadas"
                subtitulo = "Últimas actividades liquidadas"

            elif filtro_val == "cancelled":
                result, status = await APIClient.get_ctas(
                    status="cancelled", limit=limite
                )
                titulo = "❌ CTAs canceladas"
                subtitulo = "Actividades canceladas por oficiales"

            else:  # all
                result, status = await APIClient.get_ctas(limit=limite)
                titulo = "📜 Historial de CTAs"
                subtitulo = "Todas las actividades registradas"

        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar la lista de CTAs.", ephemeral=True
            )
            return

        ctas = result.get("ctas", [])
        if not ctas:
            await interaction.followup.send(
                f"📭 No hay CTAs en la categoría **{filtro_val}**."
            )
            return

        embed = discord.Embed(
            title=titulo,
            description=f"*{subtitulo}* · {len(ctas)} resultado(s)",
            color=Config.EMBED_COLOR,
        )

        for cta in ctas[:15]:
            start_iso = cta.get("start_time")
            time_str = start_iso
            try:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                ts = int(start_dt.timestamp())
                time_str = f"<t:{ts}:R>"
            except (ValueError, TypeError, AttributeError):
                pass

            comp = (cta.get("composition") or {}).get("name", "—")
            status_txt = cta.get("status", "?")
            paid = cta.get("paid", False)
            cta_id = cta.get("id", "?")

            badges = []
            if status_txt == "scheduled":
                badges.append("🕐 programada" if not paid else "💳 sin marcar")
            elif status_txt == "cancelled":
                badges.append("❌ cancelada")
            elif status_txt == "completed":
                badges.append("✅ completada")
            if paid:
                badges.append("💵 pagada")

            badge_str = " · ".join(badges) if badges else ""

            embed.add_field(
                name=f"#{cta_id} · {cta.get('title', '—')[:70]}",
                value=(
                    f"📍 {cta.get('location', '—')}\n"
                    f"🎭 {comp} · 👥 {cta.get('confirmed_count', 0)}\n"
                    f"⏰ {time_str}\n"
                    f"{badge_str}"
                ),
                inline=False,
            )

        if len(ctas) > 15:
            embed.set_footer(text=f"… y {len(ctas) - 15} más · Filtro: {filtro_val}")
        else:
            embed.set_footer(text=f"Filtro: {filtro_val}")

        await interaction.followup.send(embed=embed)

    # ================================================================== #
    #                          Error handler                              #
    # ================================================================== #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en CtasCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CtasCog(bot))