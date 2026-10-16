"""Cog de composiciones: crear, listar y eliminar composiciones de CTA."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import is_officer, sanitize_emoji

logger = logging.getLogger(__name__)


# ===================================================================== #
#                     Modal: escribir emoji manual                      #
# ===================================================================== #

class CustomEmojiModal(discord.ui.Modal, title="Escribir emoji"):
    """Modal auxiliar para usuarios que quieren pegar un emoji Unicode."""

    emoji_text: discord.ui.TextInput = discord.ui.TextInput(
        label="Emoji Unicode",
        placeholder="Ej: 🛡️ (usa Win + . o Ctrl+Cmd+Espacio)",
        min_length=1,
        max_length=32,
        required=True,
    )

    def __init__(self, picker_view: "EmojiPickerView"):
        super().__init__()
        self.picker_view = picker_view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.emoji_text).strip()
        icon = sanitize_emoji(raw)

        if not icon:
            await interaction.response.send_message(
                "❌ No es un emoji válido. Intenta con otro.\n"
                "*Nota: los emojis custom del servidor deben elegirse del "
                "selector, no escribirse como `:nombre:`*",
                ephemeral=True,
            )
            return

        # Añadir al panel
        self.picker_view.parent_view.weapons.append({
            "name": self.picker_view.weapon_name,
            "icon": icon,
        })

        try:
            if self.picker_view.parent_view.message:
                await self.picker_view.parent_view.message.edit(
                    embed=self.picker_view.parent_view.build_embed(),
                    view=self.picker_view.parent_view,
                )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar panel: %s", exc)

        await interaction.response.send_message(
            f"✅ Añadido: **{self.picker_view.weapon_name}** {icon}",
            ephemeral=True,
        )
        self.picker_view.stop()


# ===================================================================== #
#                 Vista efímera: picker de emoji                        #
# ===================================================================== #

class EmojiPickerView(discord.ui.View):
    """
    Vista efímera que muestra los emojis del servidor en un Select.

    Opciones de UX:
      - Select con emojis custom del servidor (hasta 25).
      - Botón "Sin emoji" → añade el arma sin icono.
      - Botón "Escribir otro" → abre un modal para pegar un Unicode.
      - Botón "Cancelar" → descarta la operación.
    """

    def __init__(
        self,
        parent_view: "CompositionBuilderView",
        weapon_name: str,
        author_id: int,
        guild: discord.Guild,
        timeout: int = 180,
    ):
        super().__init__(timeout=timeout)
        self.parent_view = parent_view
        self.weapon_name = weapon_name
        self.author_id = author_id

        # Emojis del servidor (custom). Sin filtro de animated: Discord los acepta.
        server_emojis = list(guild.emojis)[:25]

        if server_emojis:
            options = [
                discord.SelectOption(
                    label=e.name[:90],
                    value=str(e),          # "<:name:id>" o "<a:name:id>"
                    emoji=e,               # discord.py lo renderiza
                )
                for e in server_emojis
            ]
            select = discord.ui.Select(
                placeholder=f"Elige un emoji para «{weapon_name}»...",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )
            select.callback = self._on_emoji_selected
            self.add_item(select)
        else:
            # Sin emojis en el servidor: informar
            no_emoji_info = discord.ui.Button(
                label="Sin emojis disponibles en el servidor",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=0,
            )
            self.add_item(no_emoji_info)

        # Fila 1: botones auxiliares
        no_emoji_btn = discord.ui.Button(
            label="Sin emoji",
            style=discord.ButtonStyle.secondary,
            emoji="🚫",
            row=1,
        )
        no_emoji_btn.callback = self._on_no_emoji
        self.add_item(no_emoji_btn)

        custom_btn = discord.ui.Button(
            label="Escribir otro",
            style=discord.ButtonStyle.primary,
            emoji="⌨️",
            row=1,
        )
        custom_btn.callback = self._on_custom
        self.add_item(custom_btn)

        cancel_btn = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            row=1,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien añadió el arma puede elegir el emoji.",
                ephemeral=True,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    #                          Callbacks                                   #
    # ------------------------------------------------------------------ #

    async def _on_emoji_selected(self, interaction: discord.Interaction):
        select = self.children[0]
        await self._finish(interaction, select.values[0])

    async def _on_no_emoji(self, interaction: discord.Interaction):
        await self._finish(interaction, None)

    async def _on_custom(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CustomEmojiModal(self))

    async def _on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="❌ Añadir arma cancelado.", view=None
        )
        self.stop()

    async def _finish(self, interaction: discord.Interaction, icon: str | None):
        """Añade el arma al panel público y cierra el mensaje efímero."""
        self.parent_view.weapons.append({
            "name": self.weapon_name,
            "icon": icon,
        })

        try:
            if self.parent_view.message:
                await self.parent_view.message.edit(
                    embed=self.parent_view.build_embed(),
                    view=self.parent_view,
                )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar panel de composición: %s", exc)

        icon_display = icon if icon else "*(sin emoji)*"
        await interaction.response.edit_message(
            content=f"✅ Añadido: **{self.weapon_name}** {icon_display}",
            view=None,
        )
        self.stop()

    async def on_timeout(self):
        # Al expirar, deshabilitar todo y avisar
        for c in self.children:
            c.disabled = True
        logger.debug("EmojiPickerView expirada sin selección para «%s»", self.weapon_name)


# ===================================================================== #
#                 Modal: pedir nombre del arma                          #
# ===================================================================== #

class AddWeaponNameModal(discord.ui.Modal, title="Añadir arma"):
    """Modal que solo pide el nombre. El emoji se elige después."""

    nombre: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del arma",
        placeholder="Ej: Hallowfall",
        min_length=2,
        max_length=120,
        required=True,
    )

    def __init__(self, parent_view: "CompositionBuilderView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.nombre).strip()

        # Evitar duplicados
        if any(w["name"].lower() == name.lower() for w in self.parent_view.weapons):
            await interaction.response.send_message(
                f"⚠️ Ya añadiste **{name}**.", ephemeral=True
            )
            return

        # Abrir picker de emojis del servidor
        picker = EmojiPickerView(
            parent_view=self.parent_view,
            weapon_name=name,
            author_id=interaction.user.id,
            guild=interaction.guild,
        )
        await interaction.response.send_message(
            f"Arma: **{name}**\n\n"
            f"Elige un emoji del servidor en el menú, o usa los botones de abajo.",
            view=picker,
            ephemeral=True,
        )


# ===================================================================== #
#                    Modal: metadatos iniciales                         #
# ===================================================================== #

class CompositionMetaModal(discord.ui.Modal, title="Nueva composición"):
    nombre: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre de la composición",
        placeholder="Ej: ZvZ 20 · Bomb Squad",
        min_length=3,
        max_length=120,
        required=True,
    )
    descripcion: discord.ui.TextInput = discord.ui.TextInput(
        label="Descripción (opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="Ej: Composición para ZvZ de 20 con 2 healers",
        min_length=0,
        max_length=500,
        required=False,
    )

    def __init__(self, parent_view: "CompositionBuilderView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.name = str(self.nombre).strip()
        self.parent_view.description = str(self.descripcion or "").strip()

        try:
            if self.parent_view.message:
                await self.parent_view.message.edit(
                    embed=self.parent_view.build_embed(),
                    view=self.parent_view,
                )
            await interaction.response.send_message(
                "✅ Datos guardados. Ahora añade las armas.", ephemeral=True
            )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar panel: %s", exc)


# ===================================================================== #
#              Vista interactiva: construir una composición             #
# ===================================================================== #

class CompositionBuilderView(discord.ui.View):
    def __init__(self, author_id: int, timeout: int = 900):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.name: str | None = None
        self.description: str | None = None
        self.weapons: list[dict] = []
        self.confirmed = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien inició la composición puede modificarla.",
                ephemeral=True,
            )
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎭 Construcción de composición",
            color=Config.EMBED_COLOR,
        )

        if self.name:
            embed.description = f"**{self.name}**"
            if self.description:
                embed.description += f"\n*{self.description}*"
        else:
            embed.description = "*Pulsa «Editar datos» para poner nombre y descripción.*"

        if self.weapons:
            lines = []
            for i, w in enumerate(self.weapons, 1):
                icon = sanitize_emoji(w.get("icon")) or "•"
                lines.append(f"`{i:>2}.` {icon} **{w['name']}**")
            embed.add_field(
                name=f"⚔️ Armas ({len(self.weapons)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="⚔️ Armas (0)",
                value="*Añade al menos un arma para poder crear la composición.*",
                inline=False,
            )

        embed.set_footer(text="Timeout: 15 minutos · Solo oficiales")
        return embed

    # ------------------------------------------------------------------ #
    #                            Botones                                   #
    # ------------------------------------------------------------------ #

    @discord.ui.button(label="Editar datos", style=discord.ButtonStyle.primary, emoji="📝", row=0)
    async def edit_meta(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirmed:
            await interaction.response.send_message("⚠️ Ya confirmada.", ephemeral=True)
            return
        await interaction.response.send_modal(CompositionMetaModal(self))

    @discord.ui.button(label="Añadir arma", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_weapon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirmed:
            await interaction.response.send_message("⚠️ Ya confirmada.", ephemeral=True)
            return
        await interaction.response.send_modal(AddWeaponNameModal(self))

    @discord.ui.button(label="Deshacer última", style=discord.ButtonStyle.secondary, emoji="↩️", row=0)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirmed:
            await interaction.response.send_message("⚠️ Ya confirmada.", ephemeral=True)
            return
        if not self.weapons:
            await interaction.response.send_message(
                "ℹ️ No hay armas para deshacer.", ephemeral=True
            )
            return
        removed = self.weapons.pop()
        try:
            await interaction.message.edit(embed=self.build_embed(), view=self)
            await interaction.response.send_message(
                f"↩️ Eliminado: **{removed['name']}**", ephemeral=True
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirmed:
            await interaction.response.send_message("⚠️ Ya confirmada.", ephemeral=True)
            return

        if not self.name:
            await interaction.response.send_message(
                "❌ Falta el nombre. Pulsa «Editar datos».", ephemeral=True
            )
            return
        if not self.weapons:
            await interaction.response.send_message(
                "❌ Añade al menos un arma.", ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            result, status = await APIClient.create_composition(
                name=self.name,
                description=self.description,
                weapons=self.weapons,
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo crear la composición.')}",
                ephemeral=True,
            )
            return

        comp = result.get("composition", {})
        comp_id = comp.get("id", "?")

        self.confirmed = True
        for c in self.children:
            c.disabled = True

        embed = discord.Embed(
            title="✅ Composición creada",
            description=(
                f"**{comp.get('name', self.name)}** · ID `#{comp_id}`\n\n"
                f"Armas: **{len(self.weapons)}**"
            ),
            color=0x5CCB45,
        )
        embed.set_footer(text="Ya disponible en /cta crear")
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            pass

        logger.info(
            "Composición #%s creada por %s (armas=%d)",
            comp_id, interaction.user, len(self.weapons),
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for c in self.children:
            c.disabled = True

        embed = discord.Embed(
            title="❌ Composición cancelada",
            description="No se ha creado nada.",
            color=0xFF5252,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def on_timeout(self):
        if self.confirmed:
            return
        for c in self.children:
            c.disabled = True
        if self.message:
            try:
                embed = self.build_embed()
                embed.color = 0x6D6D6D
                embed.set_footer(text="⏰ Panel expirado.")
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class CompositionsCog(commands.Cog):
    """Gestión de composiciones de CTA."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    composicion = app_commands.Group(
        name="composicion",
        description="Gestión de composiciones de CTA",
    )

    # ------------------- /composicion listar -------------------

    @composicion.command(
        name="listar",
        description="Lista todas las composiciones activas",
    )
    async def listar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_compositions()
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar las composiciones.", ephemeral=True
            )
            return

        comps = result.get("compositions", [])
        if not comps:
            await interaction.followup.send(
                "📭 No hay composiciones activas. Usa `/composicion crear`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🎭 Composiciones activas ({len(comps)})",
            color=Config.EMBED_COLOR,
        )
        for c in comps[:20]:
            weapons = c.get("weapons") or []
            preview_parts = []
            for w in weapons[:6]:
                icon = sanitize_emoji(w.get("weapon_icon")) or "•"
                preview_parts.append(f"{icon} {w['weapon_name']}")
            preview = ", ".join(preview_parts)
            if len(weapons) > 6:
                preview += f" (+{len(weapons) - 6})"

            value = preview or "*Sin armas*"
            if c.get("description"):
                value = f"*{c['description'][:80]}*\n{value}"

            embed.add_field(
                name=f"#{c['id']} · {c['name'][:70]}",
                value=value[:1024],
                inline=False,
            )

        if len(comps) > 20:
            embed.set_footer(text=f"… y {len(comps) - 20} más")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------- /composicion ver -------------------

    @composicion.command(
        name="ver",
        description="Muestra el detalle de una composición",
    )
    @app_commands.describe(id="ID de la composición")
    async def ver(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_compositions()
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar.", ephemeral=True
            )
            return

        comp = next(
            (c for c in result.get("compositions", []) if c.get("id") == id),
            None,
        )
        if not comp:
            await interaction.followup.send(
                f"❌ Composición `#{id}` no encontrada.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🎭 {comp['name']}",
            description=comp.get("description") or "*Sin descripción*",
            color=Config.EMBED_COLOR,
        )

        weapons = comp.get("weapons") or []
        if weapons:
            lines = []
            for i, w in enumerate(weapons, 1):
                icon = sanitize_emoji(w.get("weapon_icon")) or "•"
                lines.append(f"`{i:>2}.` {icon} **{w['weapon_name']}**")
            embed.add_field(
                name=f"⚔️ Armas ({len(weapons)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        embed.set_footer(text=f"Composición #{comp['id']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------- /composicion crear -------------------

    @composicion.command(
        name="crear",
        description="[Oficiales] Crea una nueva composición con panel interactivo",
    )
    async def crear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden crear composiciones.", ephemeral=True
            )
            return

        view = CompositionBuilderView(author_id=interaction.user.id)
        panel_msg = await interaction.followup.send(
            embed=view.build_embed(), view=view
        )
        view.message = panel_msg

    # ------------------- /composicion eliminar -------------------

    @composicion.command(
        name="eliminar",
        description="[Oficiales] Desactiva una composición (soft delete)",
    )
    @app_commands.describe(id="ID de la composición a desactivar")
    async def eliminar(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales.", ephemeral=True
            )
            return

        try:
            result, status = await APIClient.delete_composition(id)
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo desactivar.')}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Composición `#{id}` desactivada. Las CTAs existentes siguen "
            f"funcionando pero no podrás crear nuevas con ella.",
            ephemeral=True,
        )
        logger.info("Composición #%s desactivada por %s", id, interaction.user)

    # ================================================================== #
    #                          Error handler                              #
    # ================================================================== #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en CompositionsCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CompositionsCog(bot))