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
#              Modal: añadir un arma a la composición                   #
# ===================================================================== #

class AddWeaponModal(discord.ui.Modal, title="Añadir arma"):
    nombre: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del arma",
        placeholder="Ej: Hallowfall",
        min_length=2,
        max_length=120,
        required=True,
    )
    icono: discord.ui.TextInput = discord.ui.TextInput(
        label="Emoji (opcional, uno solo)",
        placeholder="Ej: 🛡️",
        min_length=0,
        max_length=32,          # ← era 8, ahora 32 (ZWJ sequences pueden ser largos)
        required=False,
    )

    def __init__(self, parent_view: "CompositionBuilderView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.nombre).strip()
        icon_raw = str(self.icono or "").strip()

        # Sanitizar emoji (devuelve None si no es válido)
        icon = sanitize_emoji(icon_raw) if icon_raw else None

        # Si el usuario escribió algo pero no es un emoji válido → avisar
        if icon_raw and not icon:
            await interaction.response.send_message(
                "⚠️ El emoji no es válido. Usa **un solo emoji Unicode** "
                "(no texto, no `:nombre:`, no varios emojis juntos).\n"
                "Ejemplos válidos: `🛡️` · `⚔️` · `✨` · `🏹`\n"
                "El arma se guardará **sin emoji** si continúas sin corregirlo.",
                ephemeral=True,
            )
            # No abortamos: dejamos que continúe sin emoji
            # Si prefieres abortar, cambia a `return` aquí.

        # Evitar duplicados (case-insensitive)
        if any(w["name"].lower() == name.lower() for w in self.parent_view.weapons):
            await interaction.response.send_message(
                f"⚠️ Ya añadiste **{name}**.", ephemeral=True
            )
            return

        self.parent_view.weapons.append({"name": name, "icon": icon})

        try:
            if self.parent_view.message:
                await self.parent_view.message.edit(
                    embed=self.parent_view.build_embed(),
                    view=self.parent_view,
                )
            await interaction.response.send_message(
                f"✅ Añadido: **{name}**"
                + (f" {icon}" if icon else " *(sin emoji)*"),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar panel de composición: %s", exc)
    nombre: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del arma",
        placeholder="Ej: Hallowfall",
        min_length=2,
        max_length=120,
        required=True,
    )
    icono: discord.ui.TextInput = discord.ui.TextInput(
        label="Emoji (opcional)",
        placeholder="Ej: 🛡️",
        min_length=0,
        max_length=8,
        required=False,
    )

    def __init__(self, parent_view: "CompositionBuilderView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.nombre).strip()
        icon = str(self.icono or "").strip()

        # Evitar duplicados (case-insensitive)
        if any(w["name"].lower() == name.lower() for w in self.parent_view.weapons):
            await interaction.response.send_message(
                f"⚠️ Ya añadiste **{name}**.", ephemeral=True
            )
            return

        self.parent_view.weapons.append({"name": name, "icon": icon or None})

        try:
            if self.parent_view.message:
                await self.parent_view.message.edit(
                    embed=self.parent_view.build_embed(),
                    view=self.parent_view,
                )
            await interaction.response.send_message(
                f"✅ Añadido: **{name}**", ephemeral=True
            )
        except discord.HTTPException as exc:
            logger.warning("No se pudo refrescar panel de composición: %s", exc)


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
                icon = w.get("icon") or "•"
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
        await interaction.response.send_modal(AddWeaponModal(self))

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
        embed.set_footer(text=f"Ya disponible en /cta crear")
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
            preview = ", ".join(
                f"{w.get('weapon_icon') or '•'} {w['weapon_name']}"
                for w in weapons[:6]
            )
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
            lines = [
                f"`{i:>2}.` {w.get('weapon_icon') or '•'} **{w['weapon_name']}**"
                for i, w in enumerate(weapons, 1)
            ]
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