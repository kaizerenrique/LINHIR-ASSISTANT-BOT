"""Cog de reportes: verificación periódica de integridad de miembros."""

import io
import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import is_officer

logger = logging.getLogger(__name__)

# Snapshot de retiros (para detectar quién salió del gremio recientemente)
STATE_FILE = Path(__file__).parent.parent / "state" / "leavers_snapshot.json"

# Máximo de nombres visibles en un embed antes de adjuntar archivo
MAX_NAMES_IN_EMBED = 15


# ===================================================================== #
#                       Vista con botones de acción                     #
# ===================================================================== #

class RemoveRoleView(discord.ui.View):
    """Botones para confirmar la remoción del rol @Linhir."""

    def __init__(self, target: discord.Member, timeout: int = 7200):
        super().__init__(timeout=timeout)
        self.target = target
        self.responded = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await is_officer(interaction):
            await interaction.response.send_message(
                "❌ Solo los oficiales pueden responder a esta acción.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Quitar @Linhir", style=discord.ButtonStyle.danger, emoji="🚫")
    async def confirm_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message(
                "⚠️ Esta acción ya fue procesada.", ephemeral=True
            )
            return

        linhir_role = self.target.guild.get_role(Config.LINHIR_ROLE_ID)
        if not linhir_role:
            await interaction.response.send_message(
                "❌ No se encontró el rol @Linhir en el servidor.", ephemeral=True
            )
            return

        if linhir_role not in self.target.roles:
            await interaction.response.send_message(
                f"ℹ️ **{self.target.display_name}** ya no tiene @Linhir.", ephemeral=True
            )
        else:
            try:
                await self.target.remove_roles(
                    linhir_role,
                    reason=f"Linhir report: removido por {interaction.user}",
                )
                await interaction.response.send_message(
                    f"✅ Rol @Linhir removido de **{self.target.display_name}**.",
                    ephemeral=True,
                )
                logger.info(
                    "Rol @Linhir removido de %s por %s", self.target, interaction.user
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "⚠️ El bot no tiene permisos para remover el rol. "
                    "Verifica la jerarquía de roles.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as exc:
                logger.exception("Error removiendo rol: %s", exc)
                await interaction.response.send_message(
                    "❌ Error al remover el rol. Contacta a un administrador.",
                    ephemeral=True,
                )
                return

        self.responded = True
        self._disable_all()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Mantener", style=discord.ButtonStyle.secondary, emoji="✅")
    async def keep_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message(
                "⚠️ Esta acción ya fue procesada.", ephemeral=True
            )
            return

        self.responded = True
        self._disable_all()
        await interaction.response.send_message(
            f"👍 Se mantiene el rol @Linhir para **{self.target.display_name}**.",
            ephemeral=True,
        )
        await interaction.message.edit(view=self)
        logger.info(
            "Rol @Linhir mantenido para %s (por %s)", self.target, interaction.user
        )

    def _disable_all(self):
        for child in self.children:
            child.disabled = True


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class ReportsCog(commands.Cog):
    """Reportes automáticos de integridad de miembros."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_report.start()

    def cog_unload(self):
        self.daily_report.cancel()

    # ------------------------------------------------------------------ #
    #                    Tarea automática diaria                          #
    # ------------------------------------------------------------------ #

    @tasks.loop(time=time(hour=12, minute=0, tzinfo=timezone.utc))
    async def daily_report(self):
        try:
            await self._run_report(triggered_by="auto")
        except Exception as exc:
            logger.exception("Error en reporte diario: %s", exc)

    @daily_report.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()
        # Ajustar hora según Config
        hour, minute = Config.report_hour_and_minute()
        self.daily_report.change_interval(
            time=time(hour=hour, minute=minute, tzinfo=timezone.utc)
        )
        logger.info("Reporte diario programado a las %02d:%02d UTC", hour, minute)

    # ------------------------------------------------------------------ #
    #                          /report                                    #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="report",
        description="Genera el reporte de integridad de miembros (Solo Oficiales)",
    )
    async def report(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo los oficiales pueden ejecutar este comando.",
                ephemeral=True,
            )
            return

        result = await self._run_report(triggered_by="manual", return_data=True)
        if result is None:
            await interaction.followup.send(
                "❌ No se pudo generar el reporte. Revisa los logs.", ephemeral=True
            )
            return

        embeds, files, views, new_leavers = result

        # Enviar los embeds y archivos públicamente (no ephemeral) para que queden visibles
        await interaction.followup.send(embed=embeds[0])  # summary
        for emb in embeds[1:]:
            await interaction.followup.send(embed=emb)

        if files:
            for f in files:
                await interaction.followup.send(file=f)

        for view_data in views:
            member, character = view_data
            emb = self._build_leaver_embed(member, character)
            await interaction.followup.send(embed=emb, view=RemoveRoleView(member))

    # ------------------------------------------------------------------ #
    #                     Lógica principal del reporte                    #
    # ------------------------------------------------------------------ #

    async def _run_report(self, triggered_by: str = "manual", return_data: bool = False):
        """
        Retorna:
          - Si return_data=True: (embeds, files, views, new_leavers)
          - Si False: envía al canal y retorna None
        """
        if not self.bot.guilds:
            logger.warning("_run_report: sin guilds.")
            return None

        guild = self._pick_guild()
        if not guild:
            logger.warning("_run_report: no se encontró el guild objetivo.")
            return None

        # 1. Backend
        try:
            data, status = await APIClient.get_registered_members()
        except AuthenticationError:
            raise
        except AuthorizationError:
            raise

        if status != 200 or not data.get("success"):
            logger.error("_run_report: fallo /members status=%s", status)
            return None

        totals              = data.get("totals", {})
        guild_registered    = data.get("guild_registered", [])
        guild_unregistered  = data.get("guild_unregistered", [])
        all_linked          = data.get("all_linked", [])

        # 2. Discord
        linhir_role = guild.get_role(Config.LINHIR_ROLE_ID)
        if not linhir_role:
            logger.error("_run_report: rol @Linhir no encontrado.")
            return None

        try:
            discord_with_role = [
                m async for m in guild.fetch_members(limit=None)
                if linhir_role in m.roles and not m.bot
            ]
        except discord.HTTPException as exc:
            logger.exception("_run_report: error obteniendo miembros: %s", exc)
            return None

        discord_role_ids = {str(m.id) for m in discord_with_role}

        # 3. Cruces
        # 3a. Vinculados en BD (discord_user_id en el roster)
        linked_ids_in_guild = {str(p["discord_user_id"]) for p in guild_registered}

        # 3b. Usuarios con @Linhir que NO están vinculados a un miembro del gremio
        #     (tienen rol pero no están en la BD del gremio con ese discord_user_id)
        with_role_not_linked = [m for m in discord_with_role if str(m.id) not in linked_ids_in_guild]

        # 3c. Retiros: vinculados cuyo personaje ya no es miembro del gremio
        #     (miembro=false) PERO aún tienen @Linhir en Discord
        current_leavers = []
        for p in all_linked:
            if p["miembro"]:
                continue  # sigue siendo miembro
            uid = str(p["discord_user_id"])
            if uid in discord_role_ids:
                member = next((m for m in discord_with_role if str(m.id) == uid), None)
                if member:
                    current_leavers.append((member, p))

        # 4. Detectar retiros NUEVOS vs snapshot anterior
        new_leavers = self._detect_new_leavers(current_leavers)
        self._save_snapshot(current_leavers)

        # 5. Construir embeds
        summary = self._build_summary(
            totals=totals,
            discord_with_role=len(discord_with_role),
            with_role_not_linked=len(with_role_not_linked),
            new_leavers=len(new_leavers),
            triggered_by=triggered_by,
        )

        embeds = [summary]
        files = []

        # 5a. Sin vincular a Discord (adjuntar archivo si > MAX_NAMES_IN_EMBED)
        if guild_unregistered:
            emb, file = self._build_unregistered_embed(guild_unregistered)
            embeds.append(emb)
            if file:
                files.append(file)

        # 5b. Con @Linhir pero sin estar en la BD del gremio
        if with_role_not_linked:
            embeds.append(self._build_wrong_role_embed(with_role_not_linked))

        # 5c. Nuevos retiros → se envían con botones
        views = new_leavers if new_leavers else []

        if return_data:
            return embeds, files, views, new_leavers

        # Envío al canal
        await self._send_to_channel(embeds, files, new_leavers)
        return None

    # ------------------------------------------------------------------ #
    #                         Helpers internos                            #
    # ------------------------------------------------------------------ #

    def _pick_guild(self) -> discord.Guild | None:
        for guild in self.bot.guilds:
            if guild.get_role(Config.LINHIR_ROLE_ID):
                return guild
        return None

    def _load_snapshot(self) -> set[str]:
        if not STATE_FILE.exists():
            return set()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(data.get("leavers", []))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("No se pudo leer snapshot: %s", exc)
            return set()

    def _save_snapshot(self, leavers: list):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ids = [str(member.id) for member, _ in leavers]
        try:
            STATE_FILE.write_text(
                json.dumps(
                    {
                        "leavers": ids,
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("No se pudo guardar snapshot: %s", exc)

    def _detect_new_leavers(self, current: list) -> list:
        previous = self._load_snapshot()
        return [(member, char) for member, char in current if str(member.id) not in previous]

    async def _send_to_channel(self, embeds, files, new_leavers):
        if not Config.REPORT_CHANNEL_ID:
            logger.warning("REPORT_CHANNEL_ID no configurado. Reporte no enviado.")
            return

        channel = self.bot.get_channel(Config.REPORT_CHANNEL_ID)
        if not channel:
            logger.error("Canal de reportes no encontrado (ID: %s)", Config.REPORT_CHANNEL_ID)
            return

        try:
            # 1. Embeds
            await channel.send(embed=embeds[0])
            for emb in embeds[1:]:
                await channel.send(embed=emb)

            # 2. Archivos
            for f in files:
                await channel.send(file=f)

            # 3. Botones de retiro
            for member, character in new_leavers:
                emb = self._build_leaver_embed(member, character)
                await channel.send(embed=emb, view=RemoveRoleView(member))

        except discord.HTTPException as exc:
            logger.exception("Error enviando reporte: %s", exc)

    # ------------------------------------------------------------------ #
    #                       Constructores de embeds                       #
    # ------------------------------------------------------------------ #

    def _build_summary(
        self,
        totals: dict,
        discord_with_role: int,
        with_role_not_linked: int,
        new_leavers: int,
        triggered_by: str,
    ) -> discord.Embed:
        guild_members      = totals.get("guild_members", 0)
        guild_registered   = totals.get("guild_registered", 0)
        guild_unregistered = totals.get("guild_unregistered", 0)

        pct_reg = (guild_registered / guild_members * 100) if guild_members else 0
        pct_unreg = (guild_unregistered / guild_members * 100) if guild_members else 0

        embed = discord.Embed(
            title="📊 Reporte de Integridad — Linhir",
            color=Config.EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="🛡️ Integrantes del gremio (Albion)",
            value=f"**{guild_members}**",
            inline=False,
        )
        embed.add_field(
            name="✅ Vinculados a Discord",
            value=f"`{guild_registered}` ({pct_reg:.1f}%)",
            inline=True,
        )
        embed.add_field(
            name="⚠️ Sin vincular",
            value=f"`{guild_unregistered}` ({pct_unreg:.1f}%)",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

        embed.add_field(
            name="🎭 Usuarios con @Linhir en Discord",
            value=f"`{discord_with_role}`",
            inline=True,
        )
        embed.add_field(
            name="🚫 Con @Linhir sin ser miembros",
            value=f"`{with_role_not_linked}`",
            inline=True,
        )
        embed.add_field(
            name="🔔 Nuevos retiros",
            value=f"`{new_leavers}`",
            inline=True,
        )

        embed.set_footer(
            text=f"{Config.GUILD_NAME} · {'Automático' if triggered_by == 'auto' else 'Manual'}",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    def _build_unregistered_embed(self, unregistered: list) -> tuple[discord.Embed, discord.File | None]:
        """Embed con los integrantes del gremio que NO están vinculados a Discord."""
        total = len(unregistered)
        embed = discord.Embed(
            title=f"⚠️ Integrantes del gremio sin vincular a Discord ({total})",
            description=(
                "Estos personajes **pertenecen al gremio en Albion** pero aún no han "
                "ejecutado `/register start` para vincular su cuenta de Discord."
            ),
            color=0xE67E22,
        )

        visible = unregistered[:MAX_NAMES_IN_EMBED]
        lines = [f"• `{p['name']}`" for p in visible]
        if total > MAX_NAMES_IN_EMBED:
            lines.append(f"… y **{total - MAX_NAMES_IN_EMBED}** más (ver archivo adjunto)")

        embed.add_field(
            name="Personajes",
            value="\n".join(lines) if lines else "Ninguno",
            inline=False,
        )

        # Archivo adjunto si son muchos
        file = None
        if total > MAX_NAMES_IN_EMBED:
            content = "\n".join(f"{i+1:>3}. {p['name']}" for i, p in enumerate(unregistered))
            file = discord.File(
                io.BytesIO(content.encode("utf-8")),
                filename=f"sin_vincular_{datetime.now().strftime('%Y%m%d')}.txt",
            )

        return embed, file

    def _build_wrong_role_embed(self, members: list[discord.Member]) -> discord.Embed:
        """Usuarios con @Linhir en Discord que NO están vinculados a un miembro del gremio."""
        embed = discord.Embed(
            title=f"🚫 Usuarios con @Linhir sin ser miembros del gremio ({len(members)})",
            description=(
                "Estos usuarios tienen el rol `@Linhir` en Discord pero **no están "
                "vinculados** a un integrante del gremio en la base de datos.\n"
                "Puede ser que usaron el bot anterior, o se les asignó manualmente."
            ),
            color=0xE74C3C,
        )

        visible = members[:MAX_NAMES_IN_EMBED]
        lines = [f"• {m.mention} — `{m.display_name}`" for m in visible]
        if len(members) > MAX_NAMES_IN_EMBED:
            lines.append(f"… y **{len(members) - MAX_NAMES_IN_EMBED}** más")

        embed.add_field(
            name="Usuarios",
            value="\n".join(lines) if lines else "Ninguno",
            inline=False,
        )
        embed.set_footer(text="Acción sugerida: pedirles que usen /register o retirar el rol.")
        return embed

    def _build_leaver_embed(self, member: discord.Member, character: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🔔 Retiro del gremio detectado",
            description=(
                f"**{member.display_name}** ({member.mention}) ya no pertenece "
                f"al gremio Linhir en Albion pero aún conserva `@Linhir` en Discord."
            ),
            color=0xE67E22,
        )
        embed.add_field(name="Personaje", value=character["name"], inline=True)
        embed.add_field(
            name="Gremio actual",
            value=character.get("guild_name") or "Sin gremio",
            inline=True,
        )
        embed.set_footer(text="¿Deseas retirar el rol @Linhir?")
        return embed

    # ------------------------------------------------------------------ #
    #                        Error handler del cog                        #
    # ------------------------------------------------------------------ #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en ReportsCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportsCog(bot))