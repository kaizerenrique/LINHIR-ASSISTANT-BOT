"""Cog de recordatorios: envío masivo de DMs y anuncios por rol."""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from utils.helpers import is_officer

logger = logging.getLogger(__name__)


# ===================================================================== #
#                     Vista genérica de confirmación                    #
# ===================================================================== #

class ConfirmActionView(discord.ui.View):
    """Botones de confirmación reutilizables para acciones masivas."""

    def __init__(self, author_id: int, timeout: int = 60):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None  # None = pendiente

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo quien inició la acción puede confirmarla.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    def _disable_all(self):
        for child in self.children:
            child.disabled = True


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class RemindersCog(commands.Cog):
    """Comandos de comunicación masiva: DMs y anuncios por rol."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------ #
    #                        /recordar (DM masivo)                        #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="recordar",
        description="Envía un DM a todos los miembros con un rol (Solo Oficiales)",
    )
    @app_commands.describe(
        rol="Rol cuyos miembros recibirán el mensaje",
        mensaje="Texto del recordatorio",
    )
    async def recordar(
        self,
        interaction: discord.Interaction,
        rol: discord.Role,
        mensaje: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo los oficiales pueden ejecutar este comando.", ephemeral=True
            )
            return

        # Filtrar bots
        members = [m for m in rol.members if not m.bot]

        if not members:
            await interaction.followup.send(
                f"⚠️ No hay miembros humanos con el rol {rol.mention}.", ephemeral=True
            )
            return

        # Estimación de tiempo
        delay = Config.REMINDER_DM_DELAY
        eta_seconds = int(len(members) * delay)
        eta_str = f"{eta_seconds // 60}m {eta_seconds % 60}s" if eta_seconds >= 60 else f"{eta_seconds}s"

        # Embed de confirmación
        embed = discord.Embed(
            title="⚠️ Confirmación de envío masivo",
            description=(
                f"Vas a enviar un **mensaje privado** a **{len(members)} miembros** "
                f"con el rol {rol.mention}.\n\n"
                f"⏱️ **Tiempo estimado:** ~{eta_str}\n\n"
                f"**Mensaje a enviar:**\n>>> {mensaje[:1500]}"
            ),
            color=Config.EMBED_ACCENT,
        )
        embed.set_footer(text="La operación puede demorar varios minutos. No cierres el bot.")

        view = ConfirmActionView(interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        # Esperar decisión del usuario
        await view.wait()

        if view.confirmed is not True:
            await interaction.edit_original_response(
                content="❌ Envío cancelado.", embed=None, view=None
            )
            return

        # Ejecutar el envío
        await self._send_mass_dm(interaction, members, mensaje, rol)

    # ------------------------------------------------------------------ #
    #                       /anuncio (mención)                            #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="anuncio",
        description="Menciona a un rol en un canal con un mensaje (Solo Oficiales)",
    )
    @app_commands.describe(
        rol="Rol a mencionar",
        mensaje="Texto del anuncio",
        canal="Canal destino (por defecto, el actual)",
    )
    async def anuncio(
        self,
        interaction: discord.Interaction,
        rol: discord.Role,
        mensaje: str,
        canal: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo los oficiales pueden ejecutar este comando.", ephemeral=True
            )
            return

        target_channel = canal or interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ El canal destino debe ser un canal de texto.", ephemeral=True
            )
            return

        # Verificar permisos del bot en el canal destino
        me = interaction.guild.me
        perms = target_channel.permissions_for(me)
        if not perms.send_messages:
            await interaction.followup.send(
                f"❌ El bot no puede enviar mensajes en {target_channel.mention}.",
                ephemeral=True,
            )
            return

        # Confirmación
        embed = discord.Embed(
            title="📢 Confirmar anuncio",
            description=(
                f"**Canal:** {target_channel.mention}\n"
                f"**Rol:** {rol.mention}\n\n"
                f"**Mensaje:**\n>>> {mensaje[:1500]}"
            ),
            color=Config.EMBED_ACCENT,
        )
        view = ConfirmActionView(interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if view.confirmed is not True:
            await interaction.edit_original_response(
                content="❌ Anuncio cancelado.", embed=None, view=None
            )
            return

        # Publicar
        try:
            await target_channel.send(
                f"{rol.mention}\n\n{mensaje}",
                allowed_mentions=discord.AllowedMentions(roles=[rol]),
            )
            await interaction.edit_original_response(
                content=f"✅ Anuncio publicado en {target_channel.mention}.",
                embed=None,
                view=None,
            )
            logger.info(
                "Anuncio publicado en #%s mención a %s por %s",
                target_channel.name, rol.name, interaction.user,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    "⚠️ El bot no puede mencionar ese rol. "
                    "Activa el toggle **'Permitir que se mencione este rol'** en la "
                    "configuración del rol, o dale al bot el permiso "
                    "**Mencionar @everyone, @here y todos los roles**."
                ),
                embed=None,
                view=None,
            )
        except discord.HTTPException as exc:
            logger.exception("Error publicando anuncio: %s", exc)
            await interaction.edit_original_response(
                content=f"❌ Error al publicar: {exc}", embed=None, view=None
            )

    # ================================================================== #
    #                    Lógica interna del DM masivo                    #
    # ================================================================== #

    async def _send_mass_dm(
        self,
        interaction: discord.Interaction,
        members: list[discord.Member],
        mensaje: str,
        rol: discord.Role,
    ):
        """Envía el DM a cada miembro, actualizando el progreso en tiempo real."""
        total = len(members)
        delay = Config.REMINDER_DM_DELAY
        guild_name = interaction.guild.name if interaction.guild else Config.GUILD_NAME

        cuerpo = (
            f"**📢 Recordatorio de {guild_name}**\n\n"
            f"{mensaje}\n\n"
            f"*Fraternitas aeterna, numquam fracta — {rol.name}*"
        )

        exitosos = 0
        fallidos = 0
        fallidos_nombres: list[str] = []

        # Progreso inicial
        await interaction.edit_original_response(
            embed=self._build_progress_embed(0, total, exitosos, fallidos),
            view=None,
        )

        # Envío secuencial
        for i, member in enumerate(members, start=1):
            try:
                await member.send(cuerpo)
                exitosos += 1
            except discord.Forbidden:
                # DMs bloqueados (privacidad del usuario)
                fallidos += 1
                fallidos_nombres.append(member.display_name)
                logger.debug("DM bloqueado para %s", member)
            except discord.HTTPException as exc:
                fallidos += 1
                fallidos_nombres.append(member.display_name)
                logger.error("Error HTTP enviando DM a %s: %s", member, exc)
            except Exception as exc:
                fallidos += 1
                fallidos_nombres.append(member.display_name)
                logger.exception("Error inesperado con %s: %s", member, exc)

            # Actualizar progreso cada 10 mensajes (evita rate limit de edits)
            if i % 10 == 0 or i == total:
                try:
                    await interaction.edit_original_response(
                        embed=self._build_progress_embed(i, total, exitosos, fallidos)
                    )
                except discord.HTTPException:
                    # Silenciar: no queremos abortar el envío por un fallo de edit
                    pass

            # Delay para respetar rate limits de Discord
            if i < total:
                await asyncio.sleep(delay)

        # Reporte final
        await interaction.edit_original_response(
            embed=self._build_final_embed(total, exitosos, fallidos, fallidos_nombres)
        )

        logger.info(
            "DM masivo completado: %d OK, %d fallidos (rol=%s, por=%s)",
            exitosos, fallidos, rol.name, interaction.user,
        )

    # ------------------------------------------------------------------ #
    #                     Constructores de embeds                         #
    # ------------------------------------------------------------------ #

    def _build_progress_embed(
        self, done: int, total: int, ok: int, fail: int
    ) -> discord.Embed:
        pct = int(done / total * 100) if total else 0
        bar_len = 20
        filled = int(bar_len * done / total) if total else 0
        bar = "█" * filled + "░" * (bar_len - filled)

        embed = discord.Embed(
            title="📨 Enviando recordatorios...",
            description=f"`{bar}` **{pct}%**",
            color=Config.EMBED_ACCENT,
        )
        embed.add_field(name="Procesados", value=f"`{done}/{total}`", inline=True)
        embed.add_field(name="✅ Enviados", value=f"`{ok}`", inline=True)
        embed.add_field(name="❌ Fallidos", value=f"`{fail}`", inline=True)
        embed.set_footer(text="No cierres el bot durante el envío.")
        return embed

    def _build_final_embed(
        self, total: int, ok: int, fail: int, fail_names: list[str]
    ) -> discord.Embed:
        embed = discord.Embed(
            title="✅ Envío masivo completado",
            color=Config.EMBED_COLOR,
        )
        embed.add_field(name="Total", value=f"`{total}`", inline=True)
        embed.add_field(name="✅ Enviados", value=f"`{ok}`", inline=True)
        embed.add_field(name="❌ Fallidos", value=f"`{fail}`", inline=True)

        if fail_names:
            preview = fail_names[:10]
            txt = "\n".join(f"• {n}" for n in preview)
            if len(fail_names) > 10:
                txt += f"\n… y **{len(fail_names) - 10}** más"
            embed.add_field(
                name="Usuarios con DM cerrado",
                value=txt,
                inline=False,
            )
            embed.set_footer(
                text="Algunos usuarios bloquean DMs de bots por configuración de privacidad."
            )

        return embed

    # ------------------------------------------------------------------ #
    #                     Error handler del cog                           #
    # ------------------------------------------------------------------ #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en RemindersCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RemindersCog(bot))