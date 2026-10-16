"""Cog de CTAs: creación, inscripción, ausencias y recordatorios."""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import format_number, is_officer, parse_datetime, sanitize_emoji

logger = logging.getLogger(__name__)


# ===================================================================== #
#                       Autocompletado de composiciones                 #
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

            # Sanear emoji: si es inválido, no pasarlo a Discord
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
                    emoji=icon,          # None si es inválido → Discord lo acepta
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

        # Verificación "amistosa" local (el backend valida de nuevo con lock)
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

        # Nombre del arma elegida (buscamos en todas las armas de la vista)
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

        # Refrescar el embed público
        await self.view.refresh_public_message()


# ===================================================================== #
#              Vista con Select(s) de armas + botón Cancelar            #
# ===================================================================== #

# ===================================================================== #
#              Vista con Select(s) de armas + botón Cancelar            #
# ===================================================================== #

class WeaponSelectView(discord.ui.View):
    """
    Vista efímera con uno o varios Selects de armas.

    Si la composición tiene >25 armas, se crean múltiples Selects en
    filas separadas (hasta 4 filas = 100 armas). El botón Cancelar
    ocupa la última fila disponible.

    NOTA: el botón Cancelar NO usa el decorador @discord.ui.button porque
    se auto-registraría en la fila 0 durante super().__init__() y
    chocaría con los Selects. Se crea y añade manualmente.
    """

    MAX_WEAPONS_PER_SELECT = 25
    MAX_ROWS_FOR_SELECTS = 4    # 4 filas × 25 = 100 armas máx

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
        self.all_weapons = weapons       # guardamos TODAS para buscar por id
        self.channel_id = channel_id
        self.message_id = message_id

        # Trocear armas en chunks de 25
        chunks = [
            weapons[i:i + self.MAX_WEAPONS_PER_SELECT]
            for i in range(0, len(weapons), self.MAX_WEAPONS_PER_SELECT)
        ]

        # Limitar a las primeras 4 filas (dejamos la 5ª para el botón Cancelar)
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

        # --- Botón Cancelar creado manualmente para controlar su fila ---
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
        try:
            result, status = await APIClient.get_cta(self.cta_id)
            if status != 200 or not result.get("success"):
                return

            channel = self.bot.get_channel(self.channel_id) if hasattr(self, "bot") else None
            if channel is None or not self.message_id:
                return

            msg = await channel.fetch_message(self.message_id)
            embed = build_cta_embed(result["cta"])
            await msg.edit(embed=embed)
        except Exception as exc:
            logger.warning("No se pudo refrescar el mensaje público de la CTA: %s", exc)


# ===================================================================== #
#                 Vista pública: Unirse / Ausente (persistente)          #
# ===================================================================== #

class CtaAnnouncementView(discord.ui.View):
    """
    Vista pública del mensaje de CTA.

    Al arrancar el bot, se registra con `bot.add_view(view, message_id=...)`
    para sobrevivir reinicios.
    """

    def __init__(self, cta_id: int, timeout: int | None = None):
        super().__init__(timeout=timeout)
        self.cta_id = cta_id

        # custom_id estables por CTA (aunque también registramos por message_id)
        self.join_btn.custom_id = f"cta:join:{cta_id}"
        self.absent_btn.custom_id = f"cta:absent:{cta_id}"
        self.refresh_btn.custom_id = f"cta:refresh:{cta_id}"

    @discord.ui.button(
        label="Unirse",
        style=discord.ButtonStyle.success,
        emoji="⚔️",
        row=0,
    )
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Obtener CTA fresca para conocer composición + ocupación actual
        try:
            result, status = await APIClient.get_cta(self.cta_id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.response.send_message(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.response.send_message(
                "❌ No se pudo cargar la CTA.", ephemeral=True
            )
            return

        cta = result["cta"]
        composition = cta.get("composition") or {}
        weapons = composition.get("weapons") or []

        occupancy = {}
        for p in cta.get("participants") or []:
            if p.get("status") == "confirmed" and p.get("composition_weapon_id"):
                occupancy[str(p["composition_weapon_id"])] = p["user_name"]

        if not weapons:
            await interaction.response.send_message(
                "⚠️ Esta CTA no tiene composición con armas.", ephemeral=True
            )
            return

        view = WeaponSelectView(
            cta_id=self.cta_id,
            weapons=weapons,
            occupancy=occupancy,
            author_id=interaction.user.id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id,
        )
        view.bot = interaction.client

        await interaction.response.send_message(
            "Selecciona el arma con la que participarás:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ausente",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        row=0,
    )
    async def absent_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            result, status = await APIClient.cta_absent(
                self.cta_id, str(interaction.user.id)
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
            "🚫 Marcado como ausente. Se ha liberado tu arma (si tenías una).",
            ephemeral=True,
        )

        # Refrescar mensaje público
        await self._refresh_public(interaction)

    @discord.ui.button(
        label="🔄",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._refresh_public(interaction)

    async def _refresh_public(self, interaction: discord.Interaction):
        try:
            result, status = await APIClient.get_cta(self.cta_id)
            if status == 200 and result.get("success"):
                embed = build_cta_embed(result["cta"])
                await interaction.message.edit(embed=embed, view=self)
        except Exception as exc:
            logger.warning("No se pudo refrescar CTA #%s: %s", self.cta_id, exc)


# ===================================================================== #
#                        Constructor de embed                           #
# ===================================================================== #

def build_cta_embed(cta: dict) -> discord.Embed:
    """Construye el embed público de una CTA con ocupación de armas."""
    cta_id = cta.get("id", "?")
    title = cta.get("title", "—")
    location = cta.get("location", "—")
    status = cta.get("status", "scheduled")
    composition = cta.get("composition") or {}
    weapons = composition.get("weapons") or []
    participants = cta.get("participants") or []

    color_map = {
        "scheduled": Config.EMBED_COLOR,
        "cancelled": 0xFF5252,
        "completed": 0x6D6D6D,
    }
    color = color_map.get(status, Config.EMBED_COLOR)

    embed = discord.Embed(
        title=f"⚔️ {title}",
        description=f"📍 **{location}**\n🎭 Composición: **{composition.get('name', '—')}**",
        color=color,
    )

    # Fechas con timestamp relativo
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
        embed.add_field(name="Estado", value="✅ Completada", inline=True)

    # Ocupación de armas
    occupancy: dict[int, str | None] = {}
    for p in participants:
        if p.get("status") == "confirmed" and p.get("composition_weapon_id"):
            occupancy[p["composition_weapon_id"]] = p["user_name"]

    if weapons:
        lines = []
        for w in weapons[:20]:
            taken = occupancy.get(w["id"])
            # Sanear emoji (dato legacy podría ser inválido)
            icon = sanitize_emoji(w.get("weapon_icon")) or "•"
            name = w["weapon_name"]
            if taken:
                lines.append(f"{icon} **{name}** — {taken}")
            else:
                lines.append(f"{icon} {name} — *libre*")

        if len(weapons) > 20:
            lines.append(f"… y {len(weapons) - 20} más")

        embed.add_field(
            name=f"🎭 Armas ({len(occupancy)}/{len(weapons)} ocupadas)",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    # Confirmados
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
        embed.add_field(
            name=f"🚫 Ausentes ({len(absent)})",
            value=", ".join(p["user_name"] for p in absent[:10])[:1024],
            inline=False,
        )

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
        # Registrar vistas persistentes de CTAs activas
        await self._register_persistent_views()
        # Arrancar el poller de recordatorios
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    async def _register_persistent_views(self):
        """Re-registra las vistas de todas las CTAs activas (sobrevive reinicios)."""
        try:
            result, status = await APIClient.get_active_ctas()
        except (AuthenticationError, AuthorizationError):
            return

        if status != 200 or not result.get("success"):
            return

        ctas = result.get("ctas", [])
        registered = 0
        for cta in ctas:
            msg_id = cta.get("discord_message_id")
            ch_id = cta.get("discord_channel_id")
            if not (msg_id and ch_id):
                continue
            try:
                view = CtaAnnouncementView(cta_id=int(cta["id"]))
                self.bot.add_view(view, message_id=int(msg_id))
                registered += 1
            except Exception as exc:
                logger.warning(
                    "No se pudo registrar vista persistente para CTA #%s: %s",
                    cta.get("id"), exc,
                )

        logger.info("Vistas persistentes de CTAs registradas: %d/%d", registered, len(ctas))

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
                # Marcar como enviado para no volver a procesarla
                await APIClient.mark_reminder_sent(cta_id)
                continue

            # Timestamp relativo para el DM
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

            # Marcar como enviado aunque algunos DMs fallaran
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

        # Validar fechas
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

        # Validar composición
        try:
            composition_id = int(composicion)
        except (ValueError, TypeError):
            await interaction.followup.send(
                "❌ Composición inválida. Usa el autocompletado.", ephemeral=True
            )
            return

        recordatorio = max(1, min(120, recordatorio))

        # =============================================================
        # 1. Crear evento de Discord
        # =============================================================
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

        # =============================================================
        # 2. Enviar mensaje placeholder SIN vista (aún no tenemos cta_id)
        # =============================================================
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

        # =============================================================
        # 3. Registrar en el backend
        # =============================================================
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
            # Limpieza del evento huérfano
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

        # =============================================================
        # 4. Actualizar el mensaje con embed real + vista persistente
        # =============================================================
        try:
            cta_result, cta_status = await APIClient.get_cta(cta_id)
            if cta_status == 200 and cta_result.get("success"):
                real_embed = build_cta_embed(cta_result["cta"])
                real_view = CtaAnnouncementView(cta_id=cta_id)
                self.bot.add_view(real_view, message_id=msg.id)
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

        # Necesitamos el message_id del canal para refrescar el embed público
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

        # ---------------------------------------------------------------
        # 1. Obtener CTA ANTES de cancelarla (necesitamos los IDs de Discord)
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 2. Cancelar en backend
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 3. Eliminar el evento programado de Discord (si existe)
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 4. Actualizar el mensaje público (embed + botones deshabilitados)
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 5. Notificar por DM a los inscritos
        # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # 6. Confirmación al oficial
        # ---------------------------------------------------------------
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

    # ------------------- /ctas (lista) -------------------

    @app_commands.command(
        name="ctas",
        description="Lista las próximas CTAs programadas",
    )
    async def listar(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            result, status = await APIClient.get_active_ctas()
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
            await interaction.followup.send("📭 No hay CTAs programadas.")
            return

        embed = discord.Embed(
            title=f"⚔️ Próximas CTAs ({len(ctas)})",
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
            embed.add_field(
                name=f"#{cta['id']} · {cta['title'][:70]}",
                value=(
                    f"📍 {cta.get('location', '—')}\n"
                    f"🎭 {comp}\n"
                    f"⏰ {time_str}"
                ),
                inline=False,
            )

        embed.set_footer(text="Usa /cta ver <id> para el detalle completo")
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