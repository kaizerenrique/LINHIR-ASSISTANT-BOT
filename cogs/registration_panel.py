"""Cog del panel de registro: botón + modal persistente en #identifícate."""

import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import is_officer, parse_birthdate

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent / "state" / "registration_panel.json"
BUTTON_CUSTOM_ID = "linhir:registration_panel:open"


# ===================================================================== #
#                          Modal de registro                             #
# ===================================================================== #

class RegistrationModal(discord.ui.Modal, title="Registro de personaje"):
    personaje: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del personaje", placeholder="Ej: kaizerenrique",
        min_length=2, max_length=50, required=True,
    )
    cumpleanos: discord.ui.TextInput = discord.ui.TextInput(
        label="Cumpleaños (DD/MM/AAAA)", placeholder="Ej: 25/09/1995",
        min_length=8, max_length=10, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        birthdate_iso, error = parse_birthdate(str(self.cumpleanos))
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except discord.HTTPException as exc:
            logger.error("Modal: error obteniendo miembro: %s", exc)
            await interaction.followup.send("⚠️ No se pudo obtener tu perfil.", ephemeral=True)
            return

        try:
            result, status = await APIClient.register_character(
                character_name=str(self.personaje).strip(),
                discord_user_id=str(member.id),
                birthdate=birthdate_iso,
                discord_username=str(member),
                performed_by_discord_id=None,
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
                f"⚠️ {result.get('reason')}\n\nUsa:\n"
                f"```\n/register search personaje:{self.personaje} cumpleanos:{self.cumpleanos}\n```",
                ephemeral=True,
            )
            return

        if not result.get("success"):
            await interaction.followup.send(f"❌ {result.get('reason')}", ephemeral=True)
            return

        decision     = result["decision"]
        character    = result.get("character", {})
        allied_guild = result.get("allied_guild")
        previous     = result.get("previous_state", {})
        pending      = result.get("guild_pending_allied", False)

        identity_cog = interaction.client.get_cog("IdentityCog")
        if not identity_cog:
            await interaction.followup.send("❌ Error interno.", ephemeral=True)
            return

        try:
            actions = await identity_cog._dispatch_role_changes(
                member, decision, character,
                allied_guild=allied_guild, previous_state=previous,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ El bot no tiene permisos para cambiar tu apodo o roles.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.exception("Modal: HTTP error: %s", exc)
            await interaction.followup.send("❌ Problema aplicando cambios.", ephemeral=True)
            return

        # 1. Efímero al usuario
        await _send_ephemeral_result(interaction, decision, character, allied_guild, pending, actions)

        # 2. Auditoría
        await _notify_audit_channel(
            bot=interaction.client, member=member,
            character=character, decision=decision,
            allied_guild=allied_guild, previous_state=previous,
            actions=actions, performed_by=None,
        )


# ===================================================================== #
#                     Vista persistente con el botón                    #
# ===================================================================== #

class RegistrationButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Registrarme", style=discord.ButtonStyle.primary,
        emoji="📝", custom_id=BUTTON_CUSTOM_ID,
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if Config.IDENTIFY_CHANNEL_ID and interaction.channel_id != Config.IDENTIFY_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ Este botón solo funciona en el canal de identificación.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal())


# ===================================================================== #
#                     Estado del panel                                  #
# ===================================================================== #

def _load_panel_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No se pudo leer estado del panel: %s", exc)
        return {}


def _save_panel_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("No se pudo guardar estado del panel: %s", exc)


def _build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📝 Registro de personaje",
        description=(
            "Bienvenido a **Linhir**.\n\n"
            "Para vincular tu personaje de Albion Online con tu cuenta de Discord, "
            "pulsa el botón **Registrarme**.\n\n"
            "**¿Qué necesitas?**\n"
            "• Nombre exacto de tu personaje\n"
            "• Tu cumpleaños (DD/MM/AAAA)\n\n"
            "El proceso dura ~30 segundos. Recibirás tu rol automáticamente."
        ),
        color=Config.EMBED_COLOR,
    )
    embed.set_footer(text=f"{Config.GUILD_NAME} Assistant")
    return embed


async def _post_panel(channel: discord.TextChannel) -> discord.Message:
    return await channel.send(embed=_build_panel_embed(), view=RegistrationButtonView())


async def _delete_previous_panel(channel: discord.TextChannel) -> None:
    state = _load_panel_state()
    message_id = state.get("message_id")
    if not message_id:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.delete()
        logger.info("Panel anterior eliminado (msg_id=%s)", message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        logger.debug("No se pudo eliminar panel anterior: %s", exc)


# ===================================================================== #
#                     Confirmación efímera                              #
# ===================================================================== #

async def _send_ephemeral_result(interaction, decision, character, allied_guild, pending_allied, actions):
    if decision == "member":
        embed = discord.Embed(
            title="✅ ¡Bienvenido al gremio Linhir!",
            description=(
                f"Personaje **{character['name']}** vinculado.\n\n"
                f"**Roles asignados:** `@Linhir`, `@Publico`, `@HRL`."
            ),
            color=Config.EMBED_COLOR,
        )
    elif decision == "allied":
        tag  = (allied_guild or {}).get("tag", "").strip("[]")
        name = (allied_guild or {}).get("guild_name", "?")
        embed = discord.Embed(
            title="🤝 ¡Bienvenido, aliado de Linhir!",
            description=(
                f"Personaje **{character['name']}** vinculado como aliado.\n\n"
                f"**Gremio:** {name} `[{tag}]`\n"
                f"**Roles:** `@HRL`, `@Publico`, `@[{tag}]`."
            ),
            color=Config.EMBED_ACCENT,
        )
    else:
        desc = f"Personaje **{character['name']}** registrado.\n\n**Rol:** `@Publico`."
        if pending_allied:
            desc += ("\n\n💡 Tu gremio aparece en la alianza HRL pero aún no está "
                     "configurado. Contacta a un oficial.")
        else:
            desc += "\n\n💡 Si deseas unirte a Linhir, contacta a un oficial."
        embed = discord.Embed(title="ℹ️ Registro público completado", description=desc, color=0x4A9DEA)

    if actions.get("errors"):
        embed.add_field(name="⚠️ Avisos",
            value="\n".join(f"• {e}" for e in actions["errors"])[:1024], inline=False)

    embed.set_footer(text=f"{Config.GUILD_NAME} Assistant")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ===================================================================== #
#                     Notificación a auditoría                          #
# ===================================================================== #

async def _notify_audit_channel(bot, member, character, decision, allied_guild,
                                previous_state, actions, performed_by=None):
    if not Config.AUDIT_CHANNEL_ID:
        return

    channel = bot.get_channel(Config.AUDIT_CHANNEL_ID)
    if not channel:
        logger.warning("Canal de auditoría %s no encontrado.", Config.AUDIT_CHANNEL_ID)
        return

    colors = {"member": 0xC54B47, "allied": 0xD4B44A, "public": 0x4A9DEA}
    icons  = {"member": "🛡️", "allied": "🤝", "public": "👤"}
    labels = {"member": "Miembro Linhir", "allied": "Aliado HRL", "public": "Público"}

    embed = discord.Embed(
        title=f"{icons[decision]} Nuevo registro — {labels[decision]}",
        color=colors[decision],
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Usuario", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="Personaje", value=f"**{character.get('name', '—')}**\n`{character.get('id', '—')}`", inline=True)
    embed.add_field(name="Gremio", value=character.get("guild_name") or "Sin gremio", inline=True)
    if decision == "allied" and allied_guild:
        embed.add_field(name="Aliado",
            value=f"**{allied_guild.get('guild_name', '?')}** · `{allied_guild.get('tag', '?')}`",
            inline=False)
    embed.add_field(name="Tipo", value=f"`{decision}`", inline=True)

    if previous_state and (previous_state.get("was_member") or previous_state.get("was_allied")):
        prev = []
        if previous_state.get("was_member"):
            prev.append("era **Miembro Linhir**")
        if previous_state.get("was_allied"):
            prev.append(f"era **Aliado** de `{previous_state.get('previous_allied_guild_name', '?')}`")
        embed.add_field(name="Cambio de estado", value="Antes " + " y ".join(prev), inline=False)

    if actions.get("roles_added") or actions.get("roles_removed"):
        parts = []
        if actions.get("roles_added"):
            parts.append("**+** " + ", ".join(f"`{r}`" for r in actions["roles_added"]))
        if actions.get("roles_removed"):
            parts.append("**−** " + ", ".join(f"`{r}`" for r in actions["roles_removed"]))
        embed.add_field(name="Roles aplicados", value="\n".join(parts), inline=False)

    if actions.get("nick_changed"):
        embed.add_field(name="Nickname",
            value=f"`{actions.get('old_nick') or '—'}` → `{actions.get('new_nick')}`",
            inline=False)

    if actions.get("errors"):
        embed.add_field(name="⚠️ Errores",
            value="\n".join(f"• {e}" for e in actions["errors"])[:1024], inline=False)

    if performed_by:
        embed.add_field(name="Registrado por", value=f"<@{performed_by}>", inline=False)

    embed.set_footer(
        text=f"{Config.GUILD_NAME} Auditoría · {member.guild.name}",
        icon_url=bot.user.display_avatar.url if bot.user else None,
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        logger.error("No se pudo enviar auditoría: %s", exc)


# ===================================================================== #
#                              Cog                                       #
# ===================================================================== #

class RegistrationPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(RegistrationButtonView())
        logger.info("Vista persistente del panel de registro inicializada")

    @app_commands.command(name="setup_panel",
        description="Publica el panel de registro en el canal #identifícate (Solo Oficiales)")
    async def setup_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send("❌ Solo oficiales.", ephemeral=True)
            return

        if not Config.IDENTIFY_CHANNEL_ID:
            await interaction.followup.send("❌ Falta IDENTIFY_CHANNEL_ID en .env.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(Config.IDENTIFY_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(f"❌ Canal <#{Config.IDENTIFY_CHANNEL_ID}> inválido.", ephemeral=True)
            return

        await _delete_previous_panel(channel)

        try:
            panel_msg = await _post_panel(channel)
            try:
                await panel_msg.pin(reason="Panel de registro persistente")
            except discord.HTTPException as exc:
                logger.warning("No se pudo pinear el panel: %s", exc)

            _save_panel_state({
                "channel_id": channel.id,
                "message_id": panel_msg.id,
                "posted_at": discord.utils.utcnow().isoformat(),
            })
            await interaction.followup.send(f"✅ Panel publicado y pineado en {channel.mention}.", ephemeral=True)
            logger.info("Panel publicado en #%s", channel.name)
        except discord.HTTPException as exc:
            logger.exception("Error publicando panel: %s", exc)
            await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        logger.error("Error en RegistrationPanelCog: %s", error, exc_info=error)
        msg = "❌ Error inesperado."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RegistrationPanelCog(bot))