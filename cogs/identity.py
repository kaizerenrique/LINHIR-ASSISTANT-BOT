"""Cog de identidad: registro y gestión de personajes Albion ↔ Discord."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import is_officer, parse_birthdate

logger = logging.getLogger(__name__)


class IdentityCog(commands.Cog):
    """Registro y gestión de identidad de miembros del gremio Linhir."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ================================================================= #
    #                    Grupo: /register                               #
    # ================================================================= #

    register_group = app_commands.Group(
        name="register",
        description="Registro y gestión de identidad en el gremio Linhir",
    )

    # ------------------------------------------------------------------ #
    #                       /register start                               #
    # ------------------------------------------------------------------ #

    @register_group.command(
        name="start",
        description="Vincula tu personaje de Albion Online con tu cuenta de Discord",
    )
    @app_commands.describe(
        personaje="Nombre exacto de tu personaje en Albion Online",
        cumpleanos="Tu cumpleaños en formato DD/MM/YYYY (ej. 25/09/1995)",
    )
    async def register_start(
        self,
        interaction: discord.Interaction,
        personaje: str,
        cumpleanos: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send(
                "❌ Este comando solo funciona en el servidor de Linhir.",
                ephemeral=True,
            )
            return

        # Validar cumpleaños
        birthdate_iso, error = parse_birthdate(cumpleanos)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        # Resolver Member fresco
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send(
                "❌ No se pudo encontrar tu perfil en este servidor.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.error("Error obteniendo miembro: %s", exc)
            await interaction.followup.send(
                "⚠️ Hubo un problema al obtener tu perfil. Intenta de nuevo.",
                ephemeral=True,
            )
            return

        # Delegar a la lógica común
        await self._process_registration(
            interaction=interaction,
            target_member=member,
            personaje=personaje.strip(),
            birthdate_iso=birthdate_iso,
            performed_by=None,
        )

    # ------------------------------------------------------------------ #
    #                      /register manager                              #
    # ------------------------------------------------------------------ #

    @register_group.command(
        name="manager",
        description="Registra a otro miembro del gremio (Solo Oficiales)",
    )
    @app_commands.describe(
        miembro="Usuario de Discord a registrar (usa el selector)",
        personaje="Nombre exacto de su personaje en Albion Online",
        cumpleanos="Su cumpleaños en formato DD/MM/YYYY",
    )
    async def register_manager(
        self,
        interaction: discord.Interaction,
        miembro: discord.Member,
        personaje: str,
        cumpleanos: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send(
                "❌ Este comando solo funciona en el servidor de Linhir.",
                ephemeral=True,
            )
            return

        # Verificar permiso de oficial
        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ No tienes permisos de oficial para ejecutar este comando.",
                ephemeral=True,
            )
            return

        # No permitir registrar bots
        if miembro.bot:
            await interaction.followup.send(
                "❌ No puedes registrar a un bot.",
                ephemeral=True,
            )
            return

        # Validar cumpleaños
        birthdate_iso, error = parse_birthdate(cumpleanos)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        # Delegar a la lógica común (performed_by = quien ejecuta)
        await self._process_registration(
            interaction=interaction,
            target_member=miembro,
            personaje=personaje.strip(),
            birthdate_iso=birthdate_iso,
            performed_by=str(interaction.user.id),
        )

    # ------------------------------------------------------------------ #
    #                       /register update                              #
    # ------------------------------------------------------------------ #

    @register_group.command(
        name="update",
        description="Sincroniza tus roles según tu estado actual en Albion "
                    "(o el de otro miembro si eres oficial)",
    )
    @app_commands.describe(
        miembro="[Opcional · Solo Oficiales] Sincronizar a otro miembro",
    )
    async def register_update(
        self,
        interaction: discord.Interaction,
        miembro: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send(
                "❌ Este comando solo funciona en el servidor de Linhir.",
                ephemeral=True,
            )
            return

        # Determinar target
        if miembro is not None and miembro.id != interaction.user.id:
            # Sincronizar a otro → requiere oficial
            if not await is_officer(interaction):
                await interaction.followup.send(
                    "❌ Solo los oficiales pueden sincronizar a otros miembros.",
                    ephemeral=True,
                )
                return
            target = miembro
            is_self = False
        else:
            # Self sync
            try:
                target = await interaction.guild.fetch_member(interaction.user.id)
            except discord.HTTPException as exc:
                logger.error("Error obteniendo self member: %s", exc)
                await interaction.followup.send(
                    "⚠️ No se pudo obtener tu perfil. Intenta de nuevo.",
                    ephemeral=True,
                )
                return
            is_self = True

        if target.bot:
            await interaction.followup.send(
                "❌ No puedes sincronizar a un bot.",
                ephemeral=True,
            )
            return

        # Consultar backend
        try:
            result, status = await APIClient.sync_registration(str(target.id))
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200:
            await interaction.followup.send(
                "❌ El servidor de Linhir no está disponible. Intenta más tarde.",
                ephemeral=True,
            )
            return

        if not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo sincronizar.')}",
                ephemeral=True,
            )
            return

        # Aplicar estado
        is_member  = result["is_member"]
        character  = result.get("character", {})

        try:
            if is_member:
                await self._apply_member(target, character)
                embed = self._embed_sync_member(target, character, is_self)
            else:
                await self._apply_public(target, character, downgrade=True)
                embed = self._embed_sync_public(target, character, is_self)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            logger.error("Bot sin permisos para editar %s", target)
            await interaction.followup.send(
                "⚠️ El bot no tiene permisos para cambiar apodo/roles de este usuario. "
                "Verifica la jerarquía de roles.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.exception("Error HTTP al aplicar sync: %s", exc)
            await interaction.followup.send(
                "❌ Hubo un problema al aplicar los cambios. Contacta a un administrador.",
                ephemeral=True,
            )

    # ================================================================= #
    #                        Lógica compartida                           #
    # ================================================================= #

    async def _process_registration(
        self,
        interaction: discord.Interaction,
        target_member: discord.Member,
        personaje: str,
        birthdate_iso: str,
        performed_by: str | None,
    ):
        """Lógica común para start y manager."""
        try:
            result, status = await APIClient.register_character(
                character_name=personaje,
                discord_user_id=str(target_member.id),
                birthdate=birthdate_iso,
                discord_username=str(target_member),
                performed_by_discord_id=performed_by,
            )
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200:
            await interaction.followup.send(
                "❌ El servidor de Linhir no está disponible. Intenta más tarde.",
                ephemeral=True,
            )
            return

        if not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo procesar el registro.')}",
                ephemeral=True,
            )
            return

        decision  = result["decision"]
        character = result.get("character", {})

        try:
            if decision == "member":
                await self._apply_member(target_member, character)
                embed = self._embed_member(character, birthdate_iso, target_member)
            else:  # public
                await self._apply_public(target_member, character, downgrade=False)
                embed = self._embed_public(character, target_member)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            logger.error("Bot sin permisos para editar %s", target_member)
            await interaction.followup.send(
                "⚠️ El bot no tiene permisos para cambiar el apodo o roles de "
                f"**{target_member.display_name}**. Verifica la jerarquía de roles.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.exception("Error HTTP al aplicar registro: %s", exc)
            await interaction.followup.send(
                "❌ Hubo un problema al aplicar los cambios. Contacta a un administrador.",
                ephemeral=True,
            )

    # ================================================================= #
    #                    Helpers de aplicación de roles                  #
    # ================================================================= #

    async def _apply_member(self, member: discord.Member, character: dict):
        """Aplica nickname + roles de miembro (idempotente)."""
        prefix   = Config.NICKNAME_PREFIX
        new_nick = f"{prefix} {character['name']}"[:32]

        # Nickname solo si cambia
        if member.nick != new_nick:
            await member.edit(nick=new_nick, reason="Linhir: registro de miembro")

        # Roles (solo añadir los que faltan)
        roles_to_add = []
        linhir_role  = member.guild.get_role(Config.LINHIR_ROLE_ID)
        publico_role = member.guild.get_role(Config.PUBLICO_ROLE_ID)

        if linhir_role and linhir_role not in member.roles:
            roles_to_add.append(linhir_role)
        if publico_role and publico_role not in member.roles:
            roles_to_add.append(publico_role)

        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Linhir: registro de miembro")

        logger.info(
            "Miembro aplicado: %s → %s (añadidos: %s)",
            member, character["name"], [r.name for r in roles_to_add],
        )

    async def _apply_public(
        self, member: discord.Member, character: dict, downgrade: bool
    ):
        """
        Aplica @Publico y establece el nickname SIN prefijo [LH].

        Si `downgrade=True` (usado en /register update cuando el usuario
        salió del gremio): además quita @Linhir si lo tenía.
        """
        roles_to_add    = []
        roles_to_remove = []

        publico_role = member.guild.get_role(Config.PUBLICO_ROLE_ID)
        linhir_role  = member.guild.get_role(Config.LINHIR_ROLE_ID)

        if publico_role and publico_role not in member.roles:
            roles_to_add.append(publico_role)

        if downgrade and linhir_role and linhir_role in member.roles:
            roles_to_remove.append(linhir_role)

        # Nickname: solo el nombre del personaje, SIN prefijo [LH]
        new_nick = character["name"][:32]
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason="Linhir: registro público")
            except discord.HTTPException as exc:
                logger.warning(
                    "No se pudo actualizar nickname de %s: %s", member, exc
                )

        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Linhir: registro público")
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Linhir: salida del gremio")

        logger.info(
            "Público aplicado: %s (nick=%s, añadidos=%s, quitados=%s, downgrade=%s)",
            member, new_nick,
            [r.name for r in roles_to_add],
            [r.name for r in roles_to_remove],
            downgrade,
        )

    # ================================================================= #
    #                          Constructores de embed                    #
    # ================================================================= #

    def _embed_member(
        self, character: dict, birthdate_iso: str, target: discord.Member
    ) -> discord.Embed:
        embed = discord.Embed(
            title="✅ ¡Bienvenido al gremio Linhir!",
            description=(
                f"El personaje **{character['name']}** ha sido verificado como "
                f"miembro del gremio para {target.mention}."
            ),
            color=Config.EMBED_COLOR,
        )
        embed.add_field(
            name="🏷️ Apodo actualizado",
            value=f"`{Config.NICKNAME_PREFIX} {character['name']}`",
            inline=False,
        )
        embed.add_field(
            name="🎭 Roles asignados",
            value="@Linhir · @Publico",
            inline=False,
        )
        y, m, d = birthdate_iso.split("-")
        embed.add_field(
            name="🎂 Cumpleaños registrado",
            value=f"`{d}/{m}/{y}`",
            inline=False,
        )
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    def _embed_public(self, character: dict, target: discord.Member) -> discord.Embed:
        embed = discord.Embed(
            title="ℹ️ Registro como público",
            description=(
                f"El personaje **{character['name']}** no es miembro actual del "
                f"gremio Linhir ({target.mention})."
            ),
            color=Config.EMBED_ACCENT,
        )
        embed.add_field(
            name="🏷️ Apodo actualizado",
            value=f"`{character['name']}` (sin prefijo)",
            inline=False,
        )
        embed.add_field(
            name="🛡️ Gremio actual",
            value=character.get("guild_name") or "Sin gremio",
            inline=False,
        )
        embed.add_field(name="🎭 Rol asignado", value="@Publico", inline=False)
        embed.add_field(
            name="💡 ¿Quieres unirte a Linhir?",
            value="Solicita ingreso en el canal de **reclutamiento** o contacta a un oficial.",
            inline=False,
        )
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    def _embed_sync_member(
        self, target: discord.Member, character: dict, is_self: bool
    ) -> discord.Embed:
        who = "Tu registro" if is_self else f"Registro de {target.mention}"
        embed = discord.Embed(
            title="🔄 Sincronización completada",
            description=(
                f"{who} está **al día**. Sigues siendo miembro del gremio Linhir."
                if is_self else
                f"{who} está **al día**. El usuario es miembro del gremio Linhir."
            ),
            color=Config.EMBED_COLOR,
        )
        embed.add_field(name="👤 Personaje", value=character["name"], inline=True)
        embed.add_field(
            name="🛡️ Gremio",
            value=character.get("guild_name") or "Linhir",
            inline=True,
        )
        embed.add_field(
            name="🎭 Roles",
            value="@Linhir · @Publico",
            inline=False,
        )
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    def _embed_sync_public(
        self, target: discord.Member, character: dict, is_self: bool
    ) -> discord.Embed:
        who = "Tu registro" if is_self else f"Registro de {target.mention}"
        embed = discord.Embed(
            title="🔄 Sincronización completada",
            description=(
                f"{who} ha sido degradado a **Público**. "
                "El personaje ya no pertenece al gremio Linhir."
            ),
            color=Config.EMBED_ACCENT,
        )
        embed.add_field(name="👤 Personaje", value=character["name"], inline=True)
        embed.add_field(
            name="🛡️ Gremio actual",
            value=character.get("guild_name") or "Sin gremio",
            inline=True,
        )
        embed.add_field(
            name="🏷️ Apodo actualizado",
            value=f"`{character['name']}` (sin prefijo)",
            inline=False,
        )
        embed.add_field(
            name="🎭 Roles",
            value="@Publico (se quitó @Linhir)",
            inline=False,
        )
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )
        return embed

    # ================================================================= #
    #                      Error handler del cog                         #
    # ================================================================= #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en IdentityCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(IdentityCog(bot))