"""Cog de actividades financieras: registro y distribución de silver."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import format_number, is_officer, parse_datetime

logger = logging.getLogger(__name__)


# ===================================================================== #
#                    Modal: añadir un participante                      #
# ===================================================================== #

class AddParticipantModal(discord.ui.Modal, title="Añadir participante"):
    """
    Pide el nombre del personaje y el porcentaje a asignar.

    El porcentaje es relativo: si todos tienen 100, se reparte equitativo.
    Si uno tiene 60 y otro 40, se reparte 60/40.
    """

    personaje: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del personaje",
        placeholder="Ej: kaizerenrique",
        min_length=2,
        max_length=50,
        required=True,
    )

    porcentaje: discord.ui.TextInput = discord.ui.TextInput(
        label="Porcentaje (deja 100 si es equitativo)",
        placeholder="100",
        default="100",
        min_length=1,
        max_length=5,
        required=False,
    )

    def __init__(self, parent_view: "ActivityBuilderView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.personaje).strip()
        pct_raw = str(self.porcentaje or "100").strip()

        # Validar porcentaje
        try:
            pct = float(pct_raw.replace(",", "."))
            if pct <= 0 or pct > 1000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ El porcentaje debe ser un número positivo (máx. 1000).",
                ephemeral=True,
            )
            return

        # Buscar personaje (match exacto, luego parcial)
        match = next(
            (p for p in self.parent_view.personajes
             if p["name"].lower() == name.lower()),
            None,
        )

        if match is None:
            partial = [
                p for p in self.parent_view.personajes
                if name.lower() in p["name"].lower()
            ]
            if len(partial) == 1:
                match = partial[0]
            elif len(partial) > 1:
                preview = ", ".join(f"`{p['name']}`" for p in partial[:5])
                extra = f" (+{len(partial) - 5} más)" if len(partial) > 5 else ""
                await interaction.response.send_message(
                    f"⚠️ Varios personajes coinciden: {preview}{extra}. "
                    "Sé más específico.",
                    ephemeral=True,
                )
                return
            else:
                await interaction.response.send_message(
                    f"❌ No encontré **{name}** entre los miembros vinculados. "
                    "Verifica que esté registrado con `/register start`.",
                    ephemeral=True,
                )
                return

        # Evitar duplicados
        if any(
            p["discord_user_id"] == match["discord_user_id"]
            for p in self.parent_view.participants
        ):
            await interaction.response.send_message(
                f"⚠️ **{match['name']}** ya está en la lista.", ephemeral=True
            )
            return

        # Añadir
        self.parent_view.participants.append({
            "discord_user_id": match["discord_user_id"],
            "name":            match["name"],
            "percent":         pct,
        })

        # Actualizar mensaje original
        await self.parent_view.refresh_from_modal(interaction)

        logger.debug(
            "Participante añadido al panel: %s (%.0f%%)",
            match["name"], pct,
        )


# ===================================================================== #
#              Vista interactiva: construir una actividad                #
# ===================================================================== #

class ActivityBuilderView(discord.ui.View):
    """
    Panel interactivo que el oficial completa antes de confirmar.

    Puede arrancar:
      - Vacío (uso desde /actividad registrar)
      - Con participantes precargados (uso desde /cta pagar)

    El `message` se asigna externamente después de enviar (`view.message = msg`)
    para poder editar el mensaje original desde el modal.
    """

    def __init__(
        self,
        author_id: int,
        base_payload: dict,
        personajes: list[dict],
        preseed_participants: list[dict] | None = None,
        timeout: int = 900,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.base_payload = base_payload
        self.personajes = personajes
        self.participants: list[dict] = list(preseed_participants or [])
        self.confirmed = False
        self.message: discord.Message | None = None

    # ------------------------------------------------------------------ #
    #                    Guard de autoría                                  #
    # ------------------------------------------------------------------ #

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien inició la actividad puede modificarla.",
                ephemeral=True,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    #                    Render del embed                                  #
    # ------------------------------------------------------------------ #

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📊 Construcción de actividad",
            description=f"**{self.base_payload['name']}**",
            color=Config.EMBED_COLOR,
        )
        embed.add_field(
            name="Silver líquido",
            value=f"`{format_number(self.base_payload['liquid_silver'])}`",
            inline=True,
        )
        embed.add_field(
            name="Items estimados",
            value=f"`{format_number(self.base_payload.get('items_value', 0))}`",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Fechas
        embed.add_field(
            name="Inicio (UTC)",
            value=f"`{self.base_payload['start_time'][:16].replace('T', ' ')}`",
            inline=True,
        )
        embed.add_field(
            name="Fin (UTC)",
            value=f"`{self.base_payload['end_time'][:16].replace('T', ' ')}`",
            inline=True,
        )
        if self.base_payload.get("cta_id"):
            embed.add_field(
                name="CTA vinculada",
                value=f"`#{self.base_payload['cta_id']}`",
                inline=True,
            )
        else:
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Participantes
        if self.participants:
            total_pct = sum(p["percent"] for p in self.participants)
            lines = [
                f"• **{p['name']}** — {p['percent']:.0f}%"
                for p in self.participants
            ]
            embed.add_field(
                name=f"👥 Participantes ({len(self.participants)}) · Σ {total_pct:.0f}%",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="👥 Participantes (0)",
                value="*Añade al menos uno para distribuir el botín.*",
                inline=False,
            )

        embed.set_footer(
            text="Añade participantes y confirma cuando estés listo. "
                 "Timeout: 15 minutos."
        )
        return embed

    # ------------------------------------------------------------------ #
    #                    Refresco desde modal                              #
    # ------------------------------------------------------------------ #

    async def refresh_from_modal(self, interaction: discord.Interaction):
        """
        Se llama desde `AddParticipantModal.on_submit`. La interacción del
        modal no puede editar el mensaje directamente, así que guardamos la
        referencia al mensaje y lo editamos manualmente.
        """
        try:
            if self.message:
                await self.message.edit(embed=self.build_embed(), view=self)
            await interaction.response.send_message(
                "✅ Participante añadido.", ephemeral=True
            )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar el panel: %s", exc)
            try:
                await interaction.response.send_message(
                    "⚠️ Participante añadido, pero no se pudo actualizar el panel.",
                    ephemeral=True,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #                          Botones                                     #
    # ------------------------------------------------------------------ #

    @discord.ui.button(
        label="Añadir participante",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        row=0,
    )
    async def add_participant(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.confirmed:
            await interaction.response.send_message(
                "⚠️ La actividad ya fue confirmada.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AddParticipantModal(self))

    @discord.ui.button(
        label="Quitar participante",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        row=0,
    )
    async def remove_participant_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.confirmed:
            await interaction.response.send_message(
                "⚠️ La actividad ya fue confirmada.", ephemeral=True
            )
            return

        if not self.participants:
            await interaction.response.send_message(
                "ℹ️ No hay participantes que quitar.", ephemeral=True
            )
            return

        # Select con los participantes actuales (máx 25)
        options = [
            discord.SelectOption(
                label=p["name"][:90],
                description=f"{p['percent']:.0f}%",
                value=str(i),
            )
            for i, p in enumerate(self.participants[:25])
        ]

        select = discord.ui.Select(
            placeholder="Elige el participante a quitar...",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def _on_remove(sel_interaction: discord.Interaction):
            if sel_interaction.user.id != self.author_id:
                await sel_interaction.response.send_message(
                    "❌ Solo el autor puede modificar.", ephemeral=True
                )
                return

            idx = int(select.values[0])
            removed = self.participants.pop(idx)

            try:
                if self.message:
                    await self.message.edit(embed=self.build_embed(), view=self)
                await sel_interaction.response.send_message(
                    f"🗑️ Quitado: **{removed['name']}**", ephemeral=True
                )
            except discord.HTTPException as exc:
                logger.warning("No se pudo refrescar panel tras quitar: %s", exc)

        select.callback = _on_remove

        remove_view = discord.ui.View(timeout=60)
        remove_view.add_item(select)

        await interaction.response.send_message(
            "Selecciona a quién quitar:",
            view=remove_view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Confirmar",
        style=discord.ButtonStyle.success,
        emoji="✅",
        row=1,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.confirmed:
            await interaction.response.send_message(
                "⚠️ Ya confirmada.", ephemeral=True
            )
            return

        if not self.participants:
            await interaction.response.send_message(
                "❌ Añade al menos un participante antes de confirmar.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        payload = {
            **self.base_payload,
            "participants": self.participants,
        }

        try:
            result, status = await APIClient.create_activity(payload)
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo registrar la actividad.')}",
                ephemeral=True,
            )
            return

        activity = result.get("activity", {})
        activity_id = activity.get("id", "?")

        self.confirmed = True
        self._disable_all()

        # Editar embed para reflejar el éxito
        embed = self.build_embed()
        embed.color = 0x5CCB45
        embed.set_footer(text=f"✅ Confirmada · ID #{activity_id}")

        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            pass

        logger.info(
            "Actividad #%s creada por %s (participantes=%d, silver=%d)",
            activity_id, interaction.user,
            len(self.participants), self.base_payload["liquid_silver"],
        )

    @discord.ui.button(
        label="Cancelar",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        row=1,
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.confirmed = False
        self._disable_all()

        embed = discord.Embed(
            title="❌ Actividad cancelada",
            description=f"**{self.base_payload['name']}** no fue registrada.",
            color=0xFF5252,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    # ------------------------------------------------------------------ #
    #                    Utilidad                                           #
    # ------------------------------------------------------------------ #

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    async def on_timeout(self):
        """Deshabilitar botones si nadie confirmó en 15 min."""
        if self.confirmed:
            return
        self._disable_all()
        if self.message:
            try:
                embed = self.build_embed()
                embed.color = 0x6D6D6D
                embed.set_footer(text="⏰ Panel expirado. Usa /actividad registrar de nuevo.")
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


# ===================================================================== #
#           Vista paginada: historial de actividades                     #
# ===================================================================== #

class ActivityHistoryView(discord.ui.View):
    """Navegación ◀/▶ sobre el historial de actividades."""

    def __init__(
        self,
        author_id: int,
        current_page: int = 0,
        page_size: int = 5,
        total: int = 0,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.current_page = current_page
        self.page_size = page_size
        self.total = total
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien ejecutó el comando puede navegar.", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self):
        self.prev_page.disabled = self.current_page <= 0
        self.next_page.disabled = (self.current_page + 1) * self.page_size >= self.total

    @discord.ui.button(label="◀ Anterior", style=discord.ButtonStyle.secondary)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._load_page(interaction, self.current_page - 1)

    @discord.ui.button(label="Siguiente ▶", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._load_page(interaction, self.current_page + 1)

    async def _load_page(self, interaction: discord.Interaction, page: int):
        await interaction.response.defer()
        offset = page * self.page_size

        try:
            result, status = await APIClient.get_activities(
                limit=self.page_size, offset=offset
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo cargar la página.", ephemeral=True
            )
            return

        self.current_page = page
        self.total = result.get("total", 0)
        activities = result.get("activities", [])

        embed = build_history_embed(activities, page, self.page_size, self.total)
        self._update_buttons()
        await interaction.edit_original_response(embed=embed, view=self)


def build_history_embed(
    activities: list, page: int, page_size: int, total: int
) -> discord.Embed:
    """Construye el embed de una página del historial."""
    embed = discord.Embed(
        title="📜 Historial de actividades",
        color=Config.EMBED_COLOR,
    )

    if not activities:
        embed.description = "*Sin resultados.*"
        return embed

    for a in activities:
        aid = a.get("id", "?")
        name = a.get("name", "—")
        start = (a.get("start_time") or "")[:16].replace("T", " ")
        liquid = a.get("liquid_silver", 0)
        items = a.get("items_value", 0)
        total_dist = a.get("total_distributed", 0)
        pro_guild = a.get("pro_guild", False)
        participants = a.get("participants") or []
        cta = a.get("cta") or {}

        badge = "🏛️ Pro-gremio" if pro_guild else f"👥 {len(participants)} part."

        value_lines = [
            f"`{start} UTC`",
            f"Líquido: `{format_number(liquid)}` · Items: `{format_number(items)}`",
        ]
        if not pro_guild:
            value_lines.append(f"Distribuido: `{format_number(total_dist)}`")
        if cta.get("title"):
            value_lines.append(f"CTA: *{cta['title'][:40]}*")

        embed.add_field(
            name=f"#{aid} · {name[:70]} · {badge}",
            value="\n".join(value_lines),
            inline=False,
        )

    # Footer con paginación
    start_i = page * page_size + 1
    end_i = min((page + 1) * page_size, total)
    total_pages = (total + page_size - 1) // page_size if total else 1
    embed.set_footer(
        text=f"{Config.GUILD_NAME} · Mostrando {start_i}-{end_i} de {total} "
             f"(página {page + 1}/{total_pages})"
    )
    return embed


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class ActivitiesCog(commands.Cog):
    """Comandos de actividades financieras del gremio."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ================================================================== #
    #                    Grupo: /actividad                                #
    # ================================================================== #

    actividad = app_commands.Group(
        name="actividad",
        description="Registro y consulta de actividades financieras",
    )

    # ------------------- /actividad registrar -------------------

    @actividad.command(
        name="registrar",
        description="[Oficiales] Registra una actividad manualmente (sin CTA)",
    )
    @app_commands.describe(
        nombre="Nombre de la actividad (ej: 'Fame Farm Rojo')",
        silver="Silver líquido recolectado",
        inicio="Inicio en formato DD/MM/YYYY HH:MM (UTC)",
        fin="Fin en formato DD/MM/YYYY HH:MM (UTC)",
        items="Valor estimado de los items (opcional)",
        pro_gremio="Si es True, todo el botín va a la cuenta del gremio",
    )
    async def registrar(
        self,
        interaction: discord.Interaction,
        nombre: str,
        silver: int,
        inicio: str,
        fin: str,
        items: int = 0,
        pro_gremio: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden registrar actividades.", ephemeral=True
            )
            return

        # Validar fecha/hora
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

        if silver < 0 or items < 0:
            await interaction.followup.send(
                "❌ Los montos no pueden ser negativos.", ephemeral=True
            )
            return

        base_payload = {
            "name":                     nombre[:120],
            "start_time":               start_iso,
            "end_time":                 end_iso,
            "liquid_silver":            int(silver),
            "items_value":              int(items),
            "pro_guild":                bool(pro_gremio),
            "registered_by_discord_id": str(interaction.user.id),
            "registered_by_name":       interaction.user.display_name,
        }

        # =============================================================
        # Modo pro-gremio: sin panel interactivo, todo directo
        # =============================================================
        if pro_gremio:
            try:
                result, status = await APIClient.create_activity(
                    {**base_payload, "participants": []}
                )
            except (AuthenticationError, AuthorizationError) as exc:
                await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
                return

            if status != 200 or not result.get("success"):
                await interaction.followup.send(
                    f"❌ {result.get('reason', 'No se pudo registrar.')}",
                    ephemeral=True,
                )
                return

            activity = result.get("activity", {})
            total_bruto = int(silver) + int(items)

            embed = discord.Embed(
                title="✅ Actividad pro-gremio registrada",
                description=(
                    f"**{nombre}**\n\n"
                    f"Silver: `{format_number(silver)}`\n"
                    f"Items: `{format_number(items)}`\n"
                    f"**Total al gremio:** `{format_number(total_bruto)}` plata"
                ),
                color=Config.EMBED_COLOR,
            )
            embed.set_footer(text=f"ID actividad #{activity.get('id', '?')}")
            await interaction.followup.send(embed=embed)
            return

        # =============================================================
        # Modo normal: cargar miembros y abrir panel interactivo
        # =============================================================
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
        if not guild_registered:
            await interaction.followup.send(
                "⚠️ No hay miembros vinculados con Discord para distribuir.",
                ephemeral=True,
            )
            return

        personajes = [
            {
                "name":            p["name"],
                "discord_user_id": str(p["discord_user_id"]),
            }
            for p in guild_registered
        ]

        view = ActivityBuilderView(
            author_id=interaction.user.id,
            base_payload=base_payload,
            personajes=personajes,
        )

        panel_msg = await interaction.followup.send(
            embed=view.build_embed(), view=view
        )
        view.message = panel_msg

    # ------------------- /actividad ver -------------------

    @actividad.command(
        name="ver",
        description="Muestra el detalle de una actividad por ID",
    )
    @app_commands.describe(id="ID de la actividad a consultar")
    async def ver(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_activity(id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'Actividad no encontrada.')}",
                ephemeral=True,
            )
            return

        activity = result.get("activity", {})
        embed = self._build_detail_embed(activity)
        await interaction.followup.send(embed=embed)

    # ------------------- /actividades (historial) -------------------

    @app_commands.command(
        name="actividades",
        description="Historial paginado de actividades financieras",
    )
    @app_commands.describe(limite="Cuántas actividades por página (5-20, default 5)")
    async def historial(
        self, interaction: discord.Interaction, limite: int = 5
    ):
        await interaction.response.defer(thinking=True)

        limite = max(5, min(20, limite))

        try:
            result, status = await APIClient.get_activities(limit=limite, offset=0)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar el historial.", ephemeral=True
            )
            return

        total = result.get("total", 0)
        activities = result.get("activities", [])

        if total == 0:
            await interaction.followup.send(
                "ℹ️ Aún no hay actividades registradas."
            )
            return

        view = ActivityHistoryView(
            author_id=interaction.user.id,
            current_page=0,
            page_size=limite,
            total=total,
        )
        embed = build_history_embed(activities, 0, limite, total)
        await interaction.followup.send(embed=embed, view=view)

    # ================================================================== #
    #                    Constructores de embeds                          #
    # ================================================================== #

    def _build_detail_embed(self, activity: dict) -> discord.Embed:
        """Embed de detalle de una actividad individual."""
        aid = activity.get("id", "?")
        name = activity.get("name", "—")
        pro_guild = activity.get("pro_guild", False)

        embed = discord.Embed(
            title=f"📊 Actividad #{aid}",
            description=f"**{name}**",
            color=Config.EMBED_COLOR,
        )

        # Fechas
        start = (activity.get("start_time") or "")[:16].replace("T", " ")
        end = (activity.get("end_time") or "")[:16].replace("T", " ")
        embed.add_field(name="Inicio (UTC)", value=f"`{start}`", inline=True)
        embed.add_field(name="Fin (UTC)", value=f"`{end}`", inline=True)
        embed.add_field(
            name="Modo",
            value="🏛️ Pro-gremio" if pro_guild else "👥 Distribución",
            inline=True,
        )

        # Montos
        embed.add_field(
            name="Silver líquido",
            value=f"`{format_number(activity.get('liquid_silver', 0))}`",
            inline=True,
        )
        embed.add_field(
            name="Valor items",
            value=f"`{format_number(activity.get('items_value', 0))}`",
            inline=True,
        )

        if pro_guild:
            embed.add_field(
                name="Total al gremio",
                value=f"`{format_number(activity.get('liquid_silver', 0) + activity.get('items_value', 0))}`",
                inline=True,
            )
        else:
            embed.add_field(
                name="Total distribuido",
                value=f"`{format_number(activity.get('total_distributed', 0))}`",
                inline=True,
            )
            embed.add_field(
                name=f"Impuesto gremio ({activity.get('guild_tax_rate', 0)}%)",
                value=f"`{format_number(activity.get('guild_tax_amount', 0))}`",
                inline=True,
            )
            embed.add_field(
                name=f"Impuesto gestión ({activity.get('management_tax_rate', 0)}%)",
                value=f"`{format_number(activity.get('management_tax_amount', 0))}`",
                inline=True,
            )

        # Registrado por
        embed.add_field(
            name="Registrado por",
            value=activity.get("registered_by_name", "—"),
            inline=False,
        )

        # CTA
        cta = activity.get("cta") or {}
        if cta.get("title"):
            embed.add_field(
                name="CTA vinculada",
                value=f"#{cta.get('id', '?')} · *{cta['title'][:60]}*",
                inline=False,
            )

        # Participantes
        participants = activity.get("participants") or []
        if participants:
            total_pct = sum(
                float(p.get("participation_percent", 0) or 0) for p in participants
            )
            lines = []
            for p in participants[:15]:
                pct = float(p.get("participation_percent", 0) or 0)
                share = p.get("share", 0)
                lines.append(
                    f"• **{p.get('user_name', '—')}** · {pct:.0f}% · "
                    f"`+{format_number(share)}`"
                )
            if len(participants) > 15:
                lines.append(f"… y {len(participants) - 15} más")

            embed.add_field(
                name=f"👥 Participantes ({len(participants)}) · Σ {total_pct:.0f}%",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    # ================================================================== #
    #                          Error handler                              #
    # ================================================================== #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en ActivitiesCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivitiesCog(bot))