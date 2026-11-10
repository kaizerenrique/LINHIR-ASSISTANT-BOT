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

    _allied_roles_cache: set[int] = set()
    _allied_cache_ts: float = 0.0
    _ALLIED_CACHE_TTL = 300

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    register_group = app_commands.Group(
        name="register",
        description="Registro y gestión de identidad en el gremio Linhir",
    )

    @register_group.command(name="start", description="Vincula tu personaje de Albion Online con tu cuenta de Discord")
    @app_commands.describe(
        personaje="Nombre exacto de tu personaje en Albion Online",
        cumpleanos="Tu cumpleaños en formato DD/MM/YYYY",
    )
    async def register_start(self, interaction: discord.Interaction, personaje: str, cumpleanos: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send("❌ Este comando solo funciona en el servidor de Linhir.", ephemeral=True)
            return

        birthdate_iso, error = parse_birthdate(cumpleanos)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send("❌ No se pudo encontrar tu perfil en este servidor.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            logger.error("Error obteniendo miembro: %s", exc)
            await interaction.followup.send("⚠️ Hubo un problema al obtener tu perfil. Intenta de nuevo.", ephemeral=True)
            return

        await self._process_registration(
            interaction=interaction, target_member=member,
            personaje=personaje.strip(), birthdate_iso=birthdate_iso, performed_by=None,
        )

    @register_group.command(name="search", description="Busca tu personaje por nombre (con desambiguación)")
    @app_commands.describe(
        personaje="Nombre de tu personaje",
        cumpleanos="Tu cumpleaños en formato DD/MM/YYYY",
    )
    async def register_search(self, interaction: discord.Interaction, personaje: str, cumpleanos: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send("❌ Este comando solo funciona en el servidor de Linhir.", ephemeral=True)
            return

        birthdate_iso, error = parse_birthdate(cumpleanos)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except discord.HTTPException as exc:
            logger.error("Error obteniendo miembro: %s", exc)
            await interaction.followup.send("⚠️ Hubo un problema al obtener tu perfil.", ephemeral=True)
            return

        try:
            result, status = await APIClient.search_character(personaje.strip())
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(f"❌ {result.get('reason', 'Búsqueda fallida.')}", ephemeral=True)
            return

        players = result.get("players", [])
        if not players:
            await interaction.followup.send(f"❌ No se encontró **{personaje}**.", ephemeral=True)
            return

        if len(players) == 1:
            await self._register_with_id(
                interaction=interaction, target_member=member,
                character_id=players[0]["id"], birthdate_iso=birthdate_iso, performed_by=None,
            )
            return

        await self._show_selection_menu(
            interaction=interaction, target_member=member,
            players=players, birthdate_iso=birthdate_iso, performed_by=None,
        )

    @register_group.command(name="manager", description="Registra a otro miembro del gremio (Solo Oficiales)")
    @app_commands.describe(
        miembro="Usuario de Discord a registrar",
        personaje="Nombre exacto de su personaje",
        cumpleanos="Su cumpleaños en formato DD/MM/YYYY",
    )
    async def register_manager(
        self, interaction: discord.Interaction,
        miembro: discord.Member, personaje: str, cumpleanos: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send("❌ Solo en el servidor de Linhir.", ephemeral=True)
            return

        if not await is_officer(interaction):
            await interaction.followup.send("❌ Solo oficiales.", ephemeral=True)
            return

        if miembro.bot:
            await interaction.followup.send("❌ No puedes registrar a un bot.", ephemeral=True)
            return

        birthdate_iso, error = parse_birthdate(cumpleanos)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        await self._process_registration(
            interaction=interaction, target_member=miembro,
            personaje=personaje.strip(), birthdate_iso=birthdate_iso,
            performed_by=str(interaction.user.id),
        )

    @register_group.command(name="update", description="Sincroniza tus roles según tu estado actual en Albion")
    @app_commands.describe(miembro="[Opcional · Solo Oficiales] Sincronizar a otro miembro")
    async def register_update(self, interaction: discord.Interaction, miembro: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not interaction.guild:
            await interaction.followup.send("❌ Solo en el servidor de Linhir.", ephemeral=True)
            return

        if miembro is not None and miembro.id != interaction.user.id:
            if not await is_officer(interaction):
                await interaction.followup.send("❌ Solo oficiales pueden sincronizar a otros.", ephemeral=True)
                return
            target = miembro
            is_self = False
        else:
            try:
                target = await interaction.guild.fetch_member(interaction.user.id)
            except discord.HTTPException as exc:
                logger.error("Error obteniendo self member: %s", exc)
                await interaction.followup.send("⚠️ No se pudo obtener tu perfil.", ephemeral=True)
                return
            is_self = True

        if target.bot:
            await interaction.followup.send("❌ No puedes sincronizar a un bot.", ephemeral=True)
            return

        try:
            result, status = await APIClient.sync_registration(str(target.id))
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo sincronizar.')}", ephemeral=True,
            )
            return

        decision     = result["decision"]
        character    = result.get("character", {})
        allied_guild = result.get("allied_guild")
        previous     = result.get("previous_state", {})

        try:
            actions = await self._dispatch_role_changes(
                target, decision, character,
                allied_guild=allied_guild, previous_state=previous,
            )
            embed = self._embed_sync_result(
                target, decision, character, allied_guild, is_self, actions
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            logger.error("Bot sin permisos para editar %s", target)
            await interaction.followup.send(
                "⚠️ El bot no tiene permisos para cambiar apodo/roles. Verifica jerarquía.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.exception("Error HTTP aplicando sync: %s", exc)
            await interaction.followup.send("❌ Problema aplicando cambios.", ephemeral=True)

    # ================================================================= #
    #                        Lógica compartida                           #
    # ================================================================= #

    async def _process_registration(self, interaction, target_member, personaje, birthdate_iso, performed_by):
        try:
            result, status = await APIClient.register_character(
                character_name=personaje,
                discord_user_id=str(target_member.id),
                birthdate=birthdate_iso,
                discord_username=str(target_member),
                performed_by_discord_id=performed_by,
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        # --- MANEJO DE ERRORES ---
        if status != 200:
            error_msg = "Servidor de Linhir no disponible."
            if status == 422:
                error_msg = result.get("error", "Datos inválidos. Revisa el nombre y la fecha de nacimiento.")
            elif status == 500:
                error_msg = "Error interno en el servidor (Laravel). Contacta a un administrador."
            
            await interaction.followup.send(f"❌ {error_msg}", ephemeral=True)
            return

        if not result.get("success") and result.get("ambiguous"):
            await interaction.followup.send(
                f"⚠️ {result.get('reason')}\n\nUsa:\n```\n/register search personaje:{personaje} cumpleanos:{birthdate_iso}\n```",
                ephemeral=True,
            )
            return

        if not result.get("success"):
            await interaction.followup.send(f"❌ {result.get('reason')}", ephemeral=True)
            return

        await self._apply_and_respond(
            interaction=interaction, target_member=target_member,
            result=result, birthdate_iso=birthdate_iso,
        )

    async def _register_with_id(
        self, interaction, target_member, character_id, birthdate_iso,
        performed_by, followup_override=None,
    ):
        responder = followup_override or interaction

        try:
            result, status = await APIClient.register_character(
                character_id=character_id,
                discord_user_id=str(target_member.id),
                birthdate=birthdate_iso,
                discord_username=str(target_member),
                performed_by_discord_id=performed_by,
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await responder.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        # --- MANEJO DE ERRORES ---
        if status != 200 or not result.get("success"):
            error_msg = result.get("reason") or result.get("error") or "Error desconocido."
            await responder.followup.send(f"❌ {error_msg}", ephemeral=True)
            return

        await self._apply_and_respond(
            interaction=responder, target_member=target_member,
            result=result, birthdate_iso=birthdate_iso,
        )

    async def _apply_and_respond(self, interaction, target_member, result, birthdate_iso):
        decision     = result["decision"]
        character    = result.get("character", {})
        allied_guild = result.get("allied_guild")
        previous     = result.get("previous_state", {})

        try:
            actions = await self._dispatch_role_changes(
                target_member, decision, character,
                allied_guild=allied_guild, previous_state=previous,
            )
            embed = self._embed_result(
                decision, character, allied_guild, birthdate_iso, target_member, actions
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            logger.error("Sin permisos para editar %s", target_member)
            await interaction.followup.send(
                f"⚠️ El bot no puede cambiar apodo/roles de **{target_member.display_name}**.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.exception("Error HTTP aplicando registro: %s", exc)
            await interaction.followup.send("❌ Problema aplicando cambios.", ephemeral=True)

    async def _show_selection_menu(
        self, interaction, target_member, players, birthdate_iso, performed_by,
    ):
        options = []
        for p in players[:25]:
            guild_name = p.get("guild_name") or "Sin gremio"
            kill_fame  = p.get("kill_fame", 0) or 0
            is_linhir  = guild_name.lower() == Config.GUILD_NAME.lower()
            label      = p["name"][:95] + " ✓" if is_linhir else p["name"][:100]
            description = f"Gremio: {guild_name} · Fama PvP: {kill_fame:,}"[:100]
            options.append(
                discord.SelectOption(
                    label=label, description=description,
                    value=p["id"], emoji="🛡️" if is_linhir else "⚔️",
                )
            )

        select = discord.ui.Select(
            placeholder="Selecciona tu personaje correcto...",
            min_values=1, max_values=1, options=options,
        )
        original_author_id = interaction.user.id

        async def select_callback(sel: discord.Interaction):
            if sel.user.id != original_author_id:
                await sel.response.send_message("❌ Solo quien inició el registro puede seleccionar.", ephemeral=True)
                return
            selected_id = select.values[0]
            for opt in select.options:
                opt.default = opt.value == selected_id
            select.disabled = True
            await sel.response.defer(ephemeral=True, thinking=True)
            try:
                await sel.edit_original_response(content="⏳ Registrando...", view=None)
            except discord.HTTPException:
                pass
            await self._register_with_id(
                interaction=interaction, target_member=target_member,
                character_id=selected_id, birthdate_iso=birthdate_iso,
                performed_by=performed_by, followup_override=sel,
            )

        select.callback = select_callback
        view = discord.ui.View(timeout=120)
        view.add_item(select)

        embed = discord.Embed(
            title="🔍 Múltiples personajes encontrados",
            description=(
                f"Se encontraron **{len(players)} personajes** con el nombre **{players[0]['name']}**.\n\n"
                "Selecciona **tu personaje** abajo. El 🛡️ pertenece a Linhir."
            ),
            color=Config.EMBED_ACCENT,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ================================================================= #
    #                    Aplicación de roles                            #
    # ================================================================= #

    @staticmethod
    def _build_nickname(prefix: str, name: str, max_len: int = 32) -> str:
        if not prefix:
            return name[:max_len]
        reserved = len(prefix) + 1
        if reserved + 3 > max_len:
            return prefix[:max_len]
        return f"{prefix} {name[:max_len - reserved]}"

    async def _dispatch_role_changes(
        self, member, decision, character, allied_guild=None, previous_state=None,
    ) -> dict:
        if decision == "member":
            return await self._apply_member(member, character, previous_state)
        if decision == "allied":
            return await self._apply_allied(member, character, allied_guild or {}, previous_state)
        return await self._apply_public(member, character, previous_state=previous_state)

    async def _apply_member(self, member, character, previous_state=None) -> dict:
        linhir_role  = member.guild.get_role(Config.LINHIR_ROLE_ID)
        publico_role = member.guild.get_role(Config.PUBLICO_ROLE_ID)
        hrl_role     = member.guild.get_role(Config.HRL_ROLE_ID)

        roles_to_add = [r for r in (linhir_role, publico_role, hrl_role) if r]

        allied_role_ids = await self._get_allied_role_ids()
        roles_to_remove = [r for r in member.roles if r.id in allied_role_ids]

        nick = self._build_nickname(Config.NICKNAME_PREFIX, character["name"])
        return await self._apply_role_changes(member, nick, roles_to_add, roles_to_remove, "Linhir: registro miembro")

    async def _apply_allied(self, member, character, allied_guild, previous_state=None) -> dict:
        guild_role_id = allied_guild.get("discord_role_id")
        tag           = (allied_guild.get("tag") or "").strip("[]").strip()
        guild_role    = member.guild.get_role(int(guild_role_id)) if guild_role_id else None

        hrl_role     = member.guild.get_role(Config.HRL_ROLE_ID)
        publico_role = member.guild.get_role(Config.PUBLICO_ROLE_ID)
        linhir_role  = member.guild.get_role(Config.LINHIR_ROLE_ID)

        roles_to_add = [r for r in (hrl_role, publico_role, guild_role) if r]

        allied_role_ids = await self._get_allied_role_ids()
        roles_to_remove = []
        if linhir_role and linhir_role in member.roles:
            roles_to_remove.append(linhir_role)
        for role in member.roles:
            if role.id in allied_role_ids and (not guild_role or role.id != guild_role.id):
                roles_to_remove.append(role)

        nick = self._build_nickname(f"[{tag}]" if tag else "", character["name"])
        return await self._apply_role_changes(
            member, nick, roles_to_add, roles_to_remove,
            f"Linhir: aliado ({allied_guild.get('guild_name', '?')})",
        )

    async def _apply_public(self, member, character, previous_state=None) -> dict:
        publico_role = member.guild.get_role(Config.PUBLICO_ROLE_ID)
        linhir_role  = member.guild.get_role(Config.LINHIR_ROLE_ID)
        hrl_role     = member.guild.get_role(Config.HRL_ROLE_ID)

        roles_to_add = [publico_role] if publico_role else []
        roles_to_remove = []
        if linhir_role and linhir_role in member.roles:
            roles_to_remove.append(linhir_role)
        if hrl_role and hrl_role in member.roles:
            roles_to_remove.append(hrl_role)

        allied_role_ids = await self._get_allied_role_ids()
        for role in member.roles:
            if role.id in allied_role_ids:
                roles_to_remove.append(role)

        nick = self._build_nickname("", character["name"])
        return await self._apply_role_changes(member, nick, roles_to_add, roles_to_remove, "Linhir: público")

    async def _apply_role_changes(
        self, member, new_nick, roles_to_add, roles_to_remove, reason,
    ) -> dict:
        actions = {
            "nick_changed": False, "old_nick": member.nick, "new_nick": new_nick,
            "roles_added": [], "roles_removed": [], "errors": [],
        }

        roles_to_add    = [r for r in roles_to_add if r and r not in member.roles]
        roles_to_remove = [r for r in roles_to_remove if r and r in member.roles and r not in roles_to_add]

        if new_nick is not None and member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason=reason)
                actions["nick_changed"] = True
            except discord.Forbidden:
                actions["errors"].append("Sin permisos para cambiar nickname")
            except discord.HTTPException as exc:
                actions["errors"].append(f"HTTP {exc.status} al cambiar nickname")

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason=reason)
                actions["roles_added"] = [r.name for r in roles_to_add]
            except discord.Forbidden:
                actions["errors"].append("Sin permisos para añadir roles")
            except discord.HTTPException as exc:
                actions["errors"].append(f"HTTP {exc.status} al añadir roles")

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason=reason)
                actions["roles_removed"] = [r.name for r in roles_to_remove]
            except discord.Forbidden:
                actions["errors"].append("Sin permisos para remover roles")
            except discord.HTTPException as exc:
                actions["errors"].append(f"HTTP {exc.status} al remover roles")

        logger.info(
            "Roles aplicados a %s: +%s -%s (nick=%s)",
            member, actions["roles_added"], actions["roles_removed"], actions["nick_changed"],
        )
        return actions

    async def _get_allied_role_ids(self) -> set[int]:
        import time as _time
        now = _time.time()
        if now - IdentityCog._allied_cache_ts < IdentityCog._ALLIED_CACHE_TTL:
            return IdentityCog._allied_roles_cache

        try:
            result, status = await APIClient.get_allied_guilds()
            if status == 200 and result.get("success"):
                IdentityCog._allied_roles_cache = {
                    int(g["discord_role_id"])
                    for g in result.get("guilds", [])
                    if g.get("discord_role_id")
                }
                IdentityCog._allied_cache_ts = now
        except (AuthenticationError, AuthorizationError) as exc:
            logger.warning("No se pudo refrescar aliados: %s", exc)

        return IdentityCog._allied_roles_cache

    # ================================================================= #
    #                          Constructores de embed                   #
    # ================================================================= #

    def _embed_result(self, decision, character, allied_guild, birthdate_iso, target, actions):
        if decision == "member":
            embed = discord.Embed(
                title="✅ ¡Bienvenido al gremio Linhir!",
                description=f"Personaje **{character['name']}** verificado para {target.mention}.",
                color=Config.EMBED_COLOR,
            )
            embed.add_field(name="🏷️ Apodo", value=f"`{Config.NICKNAME_PREFIX} {character['name']}`", inline=False)
            embed.add_field(name="🎭 Roles", value="@Linhir · @Publico · @HRL", inline=False)
            y, m, d = birthdate_iso.split("-")
            embed.add_field(name="🎂 Cumpleaños", value=f"`{d}/{m}/{y}`", inline=False)

        elif decision == "allied":
            tag  = (allied_guild or {}).get("tag", "").strip("[]")
            name = (allied_guild or {}).get("guild_name", "?")
            embed = discord.Embed(
                title="🤝 ¡Bienvenido, aliado de Linhir!",
                description=f"Personaje **{character['name']}** vinculado como aliado.",
                color=Config.EMBED_ACCENT,
            )
            embed.add_field(name="🏛️ Gremio aliado", value=f"**{name}** · `[{tag}]`", inline=False)
            embed.add_field(name="🏷️ Apodo", value=f"`[{tag}] {character['name']}`", inline=False)
            embed.add_field(name="🎭 Roles", value=f"@HRL · @Publico · @[{tag}]", inline=False)

        else:
            embed = discord.Embed(
                title="ℹ️ Registro público completado",
                description=f"Personaje **{character['name']}** registrado ({target.mention}).",
                color=Config.EMBED_ACCENT,
            )
            embed.add_field(name="🏷️ Apodo", value=f"`{character['name']}` (sin prefijo)", inline=False)
            embed.add_field(name="🛡️ Gremio", value=character.get("guild_name") or "Sin gremio", inline=False)
            embed.add_field(name="🎭 Rol", value="@Publico", inline=False)
            embed.add_field(
                name="💡 ¿Quieres unirte?",
                value="Solicita ingreso en el canal de **reclutamiento**.",
                inline=False,
            )

        if actions.get("errors"):
            embed.add_field(
                name="⚠️ Avisos",
                value="\n".join(f"• {e}" for e in actions["errors"])[:1024],
                inline=False,
            )
        embed.set_footer(text=f"{Config.GUILD_NAME} Assistant", icon_url=self.bot.user.display_avatar.url)
        return embed

    def _embed_sync_result(self, target, decision, character, allied_guild, is_self, actions):
        who = "Tu registro" if is_self else f"Registro de {target.mention}"

        if decision == "member":
            embed = discord.Embed(title="🔄 Sincronización completada",
                description=f"{who} está al día. Miembro de Linhir.", color=Config.EMBED_COLOR)
            embed.add_field(name="👤 Personaje", value=character["name"], inline=True)
            embed.add_field(name="🛡️ Gremio", value=character.get("guild_name") or "Linhir", inline=True)
            embed.add_field(name="🎭 Roles", value="@Linhir · @Publico · @HRL", inline=False)

        elif decision == "allied":
            tag  = (allied_guild or {}).get("tag", "").strip("[]")
            name = (allied_guild or {}).get("guild_name", "?")
            embed = discord.Embed(title="🔄 Sincronización completada",
                description=f"{who} está al día. Aliado de Linhir.", color=Config.EMBED_ACCENT)
            embed.add_field(name="👤 Personaje", value=character["name"], inline=True)
            embed.add_field(name="🏛️ Gremio", value=f"{name} `[{tag}]`", inline=True)
            embed.add_field(name="🎭 Roles", value=f"@HRL · @Publico · @[{tag}]", inline=False)

        else:
            embed = discord.Embed(title="🔄 Sincronización completada",
                description=f"{who} ha sido degradado a **Público**.", color=Config.EMBED_ACCENT)
            embed.add_field(name="👤 Personaje", value=character["name"], inline=True)
            embed.add_field(name="🛡️ Gremio actual", value=character.get("guild_name") or "Sin gremio", inline=True)
            embed.add_field(name="🎭 Roles", value="@Publico", inline=False)

        if actions.get("errors"):
            embed.add_field(name="⚠️ Avisos",
                value="\n".join(f"• {e}" for e in actions["errors"])[:1024], inline=False)
        embed.set_footer(text=f"{Config.GUILD_NAME} Assistant", icon_url=self.bot.user.display_avatar.url)
        return embed

    async def cog_app_command_error(self, interaction, error):
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