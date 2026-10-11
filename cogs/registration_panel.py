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

# Estado del panel (último mensaje publicado en el canal)
STATE_FILE = Path(__file__).parent.parent / "state" / "registration_panel.json"

# custom_id del botón persistente (no cambiar sin migrar)
BUTTON_CUSTOM_ID = "linhir:registration_panel:open"


# ===================================================================== #
#                          Modal de registro                             #
# ===================================================================== #

class RegistrationModal(discord.ui.Modal, title="Registro de personaje"):
    """Modal que solicita el nombre del personaje y la fecha de cumpleaños."""

    personaje: discord.ui.TextInput = discord.ui.TextInput(
        label="Nombre del personaje",
        placeholder="Ej: kaizerenrique",
        min_length=2,
        max_length=50,
        required=True,
    )

    cumpleanos: discord.ui.TextInput = discord.ui.TextInput(
        label="Cumpleaños (DD/MM/AAAA)",
        placeholder="Ej: 25/09/1995",
        min_length=8,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Defer inmediato (procesamiento puede tardar)
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 2. Validar cumpleaños localmente
        birthdate_iso, error = parse_birthdate(str(self.cumpleanos))
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        # 3. Resolver Member fresco
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except discord.HTTPException as exc:
            logger.error("Modal: error obteniendo miembro: %s", exc)
            await interaction.followup.send(
                "⚠️ No se pudo obtener tu perfil. Intenta de nuevo.",
                ephemeral=True,
            )
            return

        # 4. Consultar backend
        try:
            result, status = await APIClient.register_character(
                character_name=str(self.personaje).strip(),
                discord_user_id=str(member.id),
                birthdate=birthdate_iso,
                discord_username=str(member),
                performed_by_discord_id=None,
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

        # 5. Nombre ambiguo → sugerir /register search
        if not result.get("success") and result.get("ambiguous"):
            await interaction.followup.send(
                f"⚠️ {result.get('reason', 'Múltiples personajes con ese nombre.')}\n\n"
                f"Usa el comando:\n"
                f"```\n/register search personaje:{self.personaje} "
                f"cumpleanos:{self.cumpleanos}\n```",
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

        # 6. Aplicar roles/nickname (reutilizar helpers del IdentityCog)
        cog = interaction.client.get_cog("IdentityCog")
        if not cog:
            logger.error("IdentityCog no encontrado. No se pueden aplicar roles.")
            await interaction.followup.send(
                "❌ Error interno: cog de identidad no disponible. Contacta a un oficial.",
                ephemeral=True,
            )
            return

        try:
            if decision == "member":
                await cog._apply_member(member, character)
            else:
                await cog._apply_public(member, character, downgrade=False)
        except discord.Forbidden:
            logger.error("Modal: sin permisos para editar %s", member)
            await interaction.followup.send(
                "⚠️ El bot no tiene permisos para cambiar tu apodo o asignarte roles. "
                "Contacta a un administrador.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.exception("Modal: error HTTP aplicando roles: %s", exc)
            await interaction.followup.send(
                "❌ Hubo un problema al aplicar los cambios. Contacta a un administrador.",
                ephemeral=True,
            )
            return

        # 7. Confirmación efímera al usuario
        if decision == "member":
            await interaction.followup.send(
                f"✅ **¡Registrado como miembro de Linhir!**\n"
                f"Personaje: **{character['name']}**\n"
                f"Se te asignaron los roles @Linhir y @Publico.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"ℹ️ **Registro público completado**\n"
                f"Personaje: **{character['name']}**\n"
                f"Tu personaje no es miembro actual del gremio.\n"
                f"Si quieres unirte, solicítalo en el canal de reclutamiento.",
                ephemeral=True,
            )

        # 8. Publicar resultado público + republicar panel
        await _post_public_result_and_repost(
            bot=interaction.client,
            channel=interaction.channel,
            member=member,
            character=character,
            decision=decision,
        )


# ===================================================================== #
#                     Vista persistente con el botón                    #
# ===================================================================== #

class RegistrationButtonView(discord.ui.View):
    """
    Vista persistente con un único botón que abre el modal.

    `timeout=None` + `custom_id` permiten que el botón sobreviva reinicios
    del bot si se registra con `bot.add_view(...)` en el `setup_hook` o
    en el `__init__` del cog.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Registrarme",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id=BUTTON_CUSTOM_ID,
    )
    async def open_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Guard: si por alguna razón el canal no está configurado, permitir igual
        if Config.IDENTIFY_CHANNEL_ID and interaction.channel_id != Config.IDENTIFY_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ Este botón solo funciona en el canal de identificación.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(RegistrationModal())


# ===================================================================== #
#                     Estado del panel (persistencia)                    #
# ===================================================================== #

def _load_panel_state() -> dict:
    """Carga el estado del último panel publicado."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No se pudo leer estado del panel: %s", exc)
        return {}


def _save_panel_state(data: dict) -> None:
    """Guarda el estado del panel."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("No se pudo guardar estado del panel: %s", exc)


# ===================================================================== #
#                     Publicación del panel                              #
# ===================================================================== #

def _build_panel_embed() -> discord.Embed:
    """Construye el embed del panel de registro."""
    embed = discord.Embed(
        title="📝 Registro de personaje",
        description=(
            "Bienvenido a **Linhir**.\n\n"
            "Para vincular tu personaje de Albion Online con tu cuenta de Discord, "
            "pulsa el botón **Registrarme** de abajo.\n\n"
            "**¿Qué necesitas?**\n"
            "• Nombre exacto de tu personaje en Albion Online\n"
            "• Tu fecha de cumpleaños en formato DD/MM/AAAA\n\n"
            "El proceso toma menos de 30 segundos. Al finalizar recibirás "
            "automáticamente el rol correspondiente."
        ),
        color=Config.EMBED_COLOR,
    )
    embed.set_footer(text=f"{Config.GUILD_NAME} Assistant")
    return embed


async def _post_panel(channel: discord.TextChannel) -> discord.Message:
    """Publica el mensaje del panel con el botón y devuelve el mensaje creado."""
    view = RegistrationButtonView()
    return await channel.send(embed=_build_panel_embed(), view=view)


async def _delete_previous_panel(
    channel: discord.TextChannel,
) -> None:
    """Elimina el panel anterior si aún existe."""
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


async def _post_public_result_and_repost(
    bot: commands.Bot,
    channel: discord.TextChannel,
    member: discord.Member,
    character: dict,
    decision: str,
) -> None:
    """
    Publica el resultado del registro en el canal y republica el panel.

    Orden:
      1. Borrar panel anterior (si existe).
      2. Publicar embed de resultado.
      3. Publicar panel nuevo.
      4. Guardar el ID del panel nuevo.
    """
    # 1. Borrar panel previo
    await _delete_previous_panel(channel)

    # 2. Embed de resultado
    if decision == "member":
        result_embed = discord.Embed(
            title="✅ Nuevo miembro registrado",
            description=(
                f"{member.mention} ha vinculado su personaje "
                f"**{character['name']}** al gremio Linhir."
            ),
            color=Config.EMBED_COLOR,
        )
    else:
        result_embed = discord.Embed(
            title="ℹ️ Registro público",
            description=(
                f"{member.mention} ha registrado el personaje "
                f"**{character['name']}** (no pertenece al gremio actualmente)."
            ),
            color=Config.EMBED_ACCENT,
        )

    try:
        await channel.send(embed=result_embed)
    except discord.HTTPException as exc:
        logger.warning("No se pudo publicar el resultado del registro: %s", exc)

    # 3. Republicar el panel
    try:
        panel_msg = await _post_panel(channel)
        _save_panel_state({
            "channel_id": channel.id,
            "message_id": panel_msg.id,
            "posted_at": discord.utils.utcnow().isoformat(),
        })
        logger.info("Panel republicado (msg_id=%s)", panel_msg.id)
    except discord.HTTPException as exc:
        logger.error("No se pudo republicar el panel: %s", exc)


# ===================================================================== #
#                              Cog                                       #
# ===================================================================== #

class RegistrationPanelCog(commands.Cog):
    """Panel de registro con botón y modal persistente en #identifícate."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Registrar la vista persistente (sobrevive reinicios del bot)
        bot.add_view(RegistrationButtonView())
        logger.info("Vista persistente del panel de registro inicializada")

    # ------------------------------------------------------------------ #
    #                    /setup_panel (Solo Oficiales)                    #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="setup_panel",
        description="Publica el panel de registro en el canal #identifícate (Solo Oficiales)",
    )
    async def setup_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo los oficiales pueden ejecutar este comando.", ephemeral=True
            )
            return

        if not Config.IDENTIFY_CHANNEL_ID:
            await interaction.followup.send(
                "❌ Falta configurar `IDENTIFY_CHANNEL_ID` en el `.env`.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(Config.IDENTIFY_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"❌ No se encontró el canal <#{Config.IDENTIFY_CHANNEL_ID}> "
                f"o no es un canal de texto.",
                ephemeral=True,
            )
            return

        # Borrar panel previo (si existe)
        await _delete_previous_panel(channel)

        # Publicar nuevo panel
        try:
            panel_msg = await _post_panel(channel)
            _save_panel_state({
                "channel_id": channel.id,
                "message_id": panel_msg.id,
                "posted_at": discord.utils.utcnow().isoformat(),
            })
            await interaction.followup.send(
                f"✅ Panel publicado en {channel.mention}.", ephemeral=True
            )
            logger.info("Panel de registro publicado en #%s", channel.name)
        except discord.HTTPException as exc:
            logger.exception("Error publicando panel: %s", exc)
            await interaction.followup.send(
                f"❌ Error al publicar el panel: {exc}", ephemeral=True
            )

    # ------------------------------------------------------------------ #
    #                    Error handler del cog                            #
    # ------------------------------------------------------------------ #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en RegistrationPanelCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(RegistrationPanelCog(bot))