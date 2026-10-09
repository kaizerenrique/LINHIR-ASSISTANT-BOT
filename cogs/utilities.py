"""Cog de comandos utilitarios: /hora, /oro."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import extract_time, format_number, next_resource_window, relative_time

logger = logging.getLogger(__name__)


_TIMEZONES = (
    ("🇻🇪 Venezuela", "venezuela"),
    ("🇦🇷 Argentina", "argentina"),
    ("🇨🇴 Colombia", "colombia"),
    ("🇵🇪 Perú", "peru"),
    ("🇨🇱 Chile", "chile"),
    ("🇲🇽 México", "mexico"),
    ("🇪🇸 España", "espana"),
)


class UtilitiesCog(commands.Cog):
    """Comandos de consulta rápida sobre el estado del juego."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------ #
    #                              /hora                                  #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="hora",
        description="Hora actual del servidor Albion y zonas del gremio",
    )
    async def hora(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            data, status = await APIClient.get_server_time()
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200 or "utc" not in data:
            await interaction.followup.send(
                "❌ No se pudo consultar la hora del servidor. Intenta de nuevo en un momento.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🕒 Hora del Servidor — Linhir",
            color=Config.EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="🔵 UTC (referencia Albion)",
            value=f"**{extract_time(data['utc'])}**",
            inline=False,
        )

        for label, key in _TIMEZONES:
            if key in data:
                embed.add_field(name=label, value=f"`{extract_time(data[key])}`", inline=True)

        embed.add_field(
            name="⛏️ Próxima ventana de recursos",
            value=next_resource_window(),
            inline=False,
        )

        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    #                               /oro                                  #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="oro",
        description="Precio actual del oro en Albion West",
    )
    async def oro(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            data, status = await APIClient.get_gold_price()
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200 or "price" not in data:
            await interaction.followup.send(
                "❌ No se pudo consultar el precio del oro. Intenta de nuevo en un momento.",
                ephemeral=True,
            )
            return

        precio = data["price"]
        ts = data.get("timestamp", "")

        embed = discord.Embed(
            title="💰 Precio del Oro — Albion West",
            description=f"## 🪙 {format_number(precio)} plata",
            color=Config.EMBED_ACCENT,
        )
        embed.add_field(name="Última actualización", value=relative_time(ts), inline=True)
        embed.add_field(name="Lectura", value=f"`{ts}`", inline=True)
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    #                       Manejo de errores del cog                      #
    # ------------------------------------------------------------------ #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en UtilitiesCog: %s", error, exc_info=error)

        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo en un momento."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(UtilitiesCog(bot))