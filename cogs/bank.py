"""Cog del banco: cuenta personal, retiros y cuenta del gremio."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api_client import APIClient, AuthenticationError, AuthorizationError
from config import Config
from utils.helpers import format_number, is_officer

logger = logging.getLogger(__name__)


# ===================================================================== #
#                     Modal de rechazo (motivo obligatorio)             #
# ===================================================================== #

class RejectReasonModal(discord.ui.Modal, title="Motivo del rechazo"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Motivo",
        placeholder="Ej: Datos bancarios incorrectos",
        min_length=3,
        max_length=255,
        required=True,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "WithdrawalActionView"):
        super().__init__()
        self.parent_view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            result, status = await APIClient.reject_withdrawal(
                self.parent_view.request_id,
                reviewer_discord_id=str(interaction.user.id),
                reviewer_name=interaction.user.display_name,
                reason=str(self.reason),
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo rechazar la solicitud.')}",
                ephemeral=True,
            )
            return

        # Marcar la vista como procesada
        self.parent_view.responded = True
        self.parent_view._disable_all()

        # Editar el mensaje original (guardado por el comando `/banco pendientes`)
        if self.parent_view.message:
            try:
                embed = (
                    self.parent_view.message.embeds[0]
                    if self.parent_view.message.embeds
                    else None
                )
                if embed:
                    embed.color = 0xFF5252
                    embed.set_footer(
                        text=f"❌ Rechazado por {interaction.user.display_name}"
                    )
                await self.parent_view.message.edit(
                    embed=embed, view=self.parent_view
                )
            except discord.HTTPException as exc:
                logger.warning(
                    "No se pudo editar el mensaje original tras rechazo: %s", exc
                )

        await interaction.followup.send(
            f"✅ Solicitud #{self.parent_view.request_id} rechazada.",
            ephemeral=True,
        )

        logger.info(
            "Retiro #%s rechazado por %s (motivo=%s)",
            self.parent_view.request_id, interaction.user, self.reason,
        )


# ===================================================================== #
#                  Vista con botones Aprobar / Rechazar                 #
# ===================================================================== #

class WithdrawalActionView(discord.ui.View):
    """Botones para aprobar o rechazar una solicitud de retiro pendiente."""

    def __init__(self, request_id: int, author_id: int, timeout: int = 3600):
        super().__init__(timeout=timeout)
        self.request_id = request_id
        self.author_id = author_id  # quién ejecutó /banco pendientes
        self.responded = False
        # Referencia al mensaje que contiene esta vista. Se asigna externamente
        # después de enviarlo (`view.message = msg`) para poder editarlo desde
        # el modal de rechazo (que no tiene acceso a `interaction.message`).
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Solo oficiales pueden interactuar (re-validamos por si acaso)
        if not await is_officer(interaction):
            await interaction.response.send_message(
                "❌ Solo los oficiales pueden procesar retiros.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message(
                "⚠️ Esta solicitud ya fue procesada.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=False)

        try:
            result, status = await APIClient.approve_withdrawal(
                self.request_id,
                reviewer_discord_id=str(interaction.user.id),
                reviewer_name=interaction.user.display_name,
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo aprobar la solicitud.')}",
                ephemeral=True,
            )
            return

        # Marcar como procesada
        self.responded = True
        self._disable_all()

        # Editar el mensaje original
        if self.message:
            try:
                embed = self.message.embeds[0] if self.message.embeds else None
                if embed:
                    embed.color = 0x5CCB45
                    embed.set_footer(
                        text=f"✅ Aprobado por {interaction.user.display_name}"
                    )
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException as exc:
                logger.warning(
                    "No se pudo editar el mensaje original tras aprobación: %s", exc
                )

        logger.info(
            "Retiro #%s aprobado por %s", self.request_id, interaction.user
        )

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.responded:
            await interaction.response.send_message(
                "⚠️ Esta solicitud ya fue procesada.", ephemeral=True
            )
            return

        # Abrir modal para pedir el motivo
        await interaction.response.send_modal(RejectReasonModal(self))

    def _disable_all(self):
        for child in self.children:
            child.disabled = True


# ===================================================================== #
#                              Cog                                      #
# ===================================================================== #

class BankCog(commands.Cog):
    """Comandos del banco personal y cuenta gremial."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ================================================================== #
    #                     Grupo: /banco                                   #
    # ================================================================== #

    banco = app_commands.Group(name="banco", description="Banco personal y del gremio")

    # ------------------- /banco saldo -------------------

    @banco.command(name="saldo", description="Muestra tu saldo personal actual")
    async def saldo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result, status = await APIClient.get_my_balance(str(interaction.user.id))
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo consultar tu saldo.')}",
                ephemeral=True,
            )
            return

        saldo     = result.get("balance", 0)
        ingresos  = result.get("ingresos", 0)
        egresos   = result.get("egresos", 0)
        personaje = result.get("personaje_name", "—")

        embed = discord.Embed(
            title="💰 Tu cuenta personal",
            description=f"Personaje: **{personaje}**",
            color=Config.EMBED_COLOR,
        )
        embed.add_field(
            name="Saldo actual",
            value=f"## 🪙 {format_number(saldo)} plata",
            inline=False,
        )
        embed.add_field(name="✅ Total ingresos", value=f"`+{format_number(ingresos)}`", inline=True)
        embed.add_field(name="❌ Total egresos", value=f"`-{format_number(egresos)}`", inline=True)
        embed.set_footer(
            text=f"{Config.GUILD_NAME} Assistant",
            icon_url=self.bot.user.display_avatar.url,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------- /banco solicitar_retiro -------------------

    @banco.command(
        name="solicitar_retiro",
        description="Solicita un retiro de tu saldo (requiere aprobación de un oficial)",
    )
    @app_commands.describe(monto="Cantidad de plata a retirar")
    async def solicitar_retiro(
        self, interaction: discord.Interaction, monto: int
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if monto <= 0:
            await interaction.followup.send(
                "❌ El monto debe ser mayor a cero.", ephemeral=True
            )
            return

        try:
            result, status = await APIClient.create_withdrawal(
                str(interaction.user.id), monto
            )
        except AuthenticationError as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return
        except AuthorizationError as exc:
            await interaction.followup.send(f"⛔ {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                f"❌ {result.get('reason', 'No se pudo crear la solicitud.')}",
                ephemeral=True,
            )
            return

        req_id  = result.get("request_id", "?")
        saldo   = result.get("current_balance", 0)

        embed = discord.Embed(
            title="📤 Solicitud de retiro creada",
            description=(
                f"Solicitaste retirar **{format_number(monto)}** plata.\n"
                f"Tu nueva solicitud está **pendiente de aprobación** por un oficial.\n\n"
                f"**ID de solicitud:** `#{req_id}`\n"
                f"**Saldo restante:** `{format_number(saldo)}`"
            ),
            color=Config.EMBED_ACCENT,
        )
        embed.set_footer(
            text=f"{Config.GUILD_NAME} · Los retiros se procesan en 24-48h"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(
            "Retiro #%s creado por %s (monto=%s)",
            req_id, interaction.user, monto,
        )

    # ------------------- /banco mis_retiros -------------------

    @banco.command(
        name="mis_retiros",
        description="Historial de tus solicitudes de retiro",
    )
    @app_commands.describe(limite="Cuántas solicitudes mostrar (por defecto 10)")
    async def mis_retiros(
        self, interaction: discord.Interaction, limite: int = 10
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        limite = max(1, min(25, limite))

        try:
            result, status = await APIClient.get_my_withdrawals(
                str(interaction.user.id), limite
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar tu historial.", ephemeral=True
            )
            return

        requests = result.get("requests", [])
        if not requests:
            await interaction.followup.send(
                "ℹ️ Aún no tienes solicitudes de retiro.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📜 Tus retiros recientes",
            color=Config.EMBED_COLOR,
        )

        for req in requests:
            status_map = {
                "pending":  "⏳ Pendiente",
                "approved": "✅ Aprobado",
                "rejected": "❌ Rechazado",
            }
            label = status_map.get(req.get("status"), req.get("status", "?"))
            fecha = (req.get("created_at") or "")[:10]
            monto = format_number(req.get("amount", 0))

            line = f"`#{req['id']}` · {monto} · {label} · `{fecha}`"
            if req.get("reviewed_by_name"):
                line += f"\n  └ por **{req['reviewed_by_name']}**"
            if req.get("notes"):
                line += f"\n  └ *{req['notes'][:80]}*"

            embed.add_field(name="\u200b", value=line, inline=False)

        embed.set_footer(
            text=f"{Config.GUILD_NAME} · {len(requests)} resultado(s)"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ================================================================== #
    #              Comandos de oficial (mismo grupo /banco)               #
    # ================================================================== #

    @banco.command(
        name="pendientes",
        description="[Oficiales] Lista las solicitudes de retiro pendientes",
    )
    async def pendientes(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden ver esta lista.", ephemeral=True
            )
            return

        try:
            result, status = await APIClient.get_pending_withdrawals()
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar la lista.", ephemeral=True
            )
            return

        requests = result.get("requests", [])
        if not requests:
            await interaction.followup.send(
                "✅ No hay solicitudes pendientes.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"📋 **{len(requests)}** solicitud(es) pendiente(s) procesadas a continuación.",
            ephemeral=True,
        )

        for req in requests:
            personaje = req.get("personaje", {}).get("Name") if isinstance(req.get("personaje"), dict) else None
            user_name = req.get("user_name", "—")
            monto     = req.get("amount", 0)
            req_id    = req.get("id")
            created   = (req.get("created_at") or "")[:19].replace("T", " ")

            embed = discord.Embed(
                title=f"💸 Solicitud de retiro #{req_id}",
                description=(
                    f"**Solicitante:** {user_name}\n"
                    f"**Personaje:** {personaje or '—'}\n"
                    f"**Monto:** `{format_number(monto)}` plata\n"
                    f"**Fecha:** `{created}`"
                ),
                color=Config.EMBED_ACCENT,
            )
            embed.set_footer(text="Decide con los botones de abajo")

            view = WithdrawalActionView(
                request_id=req_id,
                author_id=interaction.user.id,
            )
            # Enviar y guardar la referencia al mensaje para poder editarlo
            # desde el modal de rechazo (que no tiene `interaction.message`).
            msg = await interaction.channel.send(embed=embed, view=view)
            view.message = msg

    # ------------------- /banco gremio_saldo -------------------

    @banco.command(
        name="gremio_saldo",
        description="[Oficiales] Muestra el saldo de la cuenta del gremio",
    )
    async def gremio_saldo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales.", ephemeral=True
            )
            return

        try:
            result, status = await APIClient.get_guild_balance()
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar el saldo.", ephemeral=True
            )
            return

        saldo = result.get("balance", 0)
        embed = discord.Embed(
            title="🏛️ Cuenta del gremio",
            description=f"## 🪙 {format_number(saldo)} plata",
            color=Config.EMBED_COLOR,
        )
        embed.set_footer(text=Config.GUILD_NAME)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------- /banco gremio_movimiento -------------------

    @banco.command(
        name="gremio_movimiento",
        description="[Oficiales] Registra un ingreso o egreso en la cuenta del gremio",
    )
    @app_commands.describe(
        tipo="inicial | ingreso | egreso",
        monto="Cantidad de plata",
        motivo="Motivo del movimiento",
        comprobante="URL de imagen de comprobante (opcional)",
    )
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Ingreso", value="ingreso"),
        app_commands.Choice(name="Egreso",  value="egreso"),
        app_commands.Choice(name="Inicial", value="inicial"),
    ])
    async def gremio_movimiento(
        self,
        interaction: discord.Interaction,
        tipo: app_commands.Choice[str],
        monto: int,
        motivo: str,
        comprobante: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales pueden registrar movimientos.", ephemeral=True
            )
            return

        if monto <= 0:
            await interaction.followup.send(
                "❌ El monto debe ser mayor a cero.", ephemeral=True
            )
            return

        try:
            result, status = await APIClient.create_guild_transaction(
                tipo=tipo.value,
                amount=monto,
                reason=motivo,
                performed_by_discord_id=str(interaction.user.id),
                performed_by_name=interaction.user.display_name,
                image_url=comprobante,
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

        nuevo_saldo = result.get("new_balance", 0)
        embed = discord.Embed(
            title="✅ Movimiento registrado",
            description=(
                f"**Tipo:** `{tipo.name}`\n"
                f"**Monto:** `{format_number(monto)}` plata\n"
                f"**Motivo:** {motivo}\n\n"
                f"**Nuevo saldo del gremio:** `{format_number(nuevo_saldo)}`"
            ),
            color=Config.EMBED_COLOR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(
            "Movimiento gremial: %s %s por %s (motivo=%s)",
            tipo.value, monto, interaction.user, motivo,
        )

    # ------------------- /banco gremio_historial -------------------

    @banco.command(
        name="gremio_historial",
        description="[Oficiales] Historial de movimientos de la cuenta del gremio",
    )
    @app_commands.describe(
        tipo="Filtrar por tipo (opcional)",
        limite="Cuántos movimientos mostrar (por defecto 15)",
    )
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Ingresos",  value="ingreso"),
        app_commands.Choice(name="Egresos",   value="egreso"),
        app_commands.Choice(name="Iniciales", value="inicial"),
    ])
    async def gremio_historial(
        self,
        interaction: discord.Interaction,
        tipo: app_commands.Choice[str] | None = None,
        limite: int = 15,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not await is_officer(interaction):
            await interaction.followup.send(
                "❌ Solo oficiales.", ephemeral=True
            )
            return

        limite = max(1, min(50, limite))
        tipo_v = tipo.value if tipo else None

        try:
            result, status = await APIClient.get_guild_transactions(
                limit=limite, offset=0, tipo=tipo_v
            )
        except (AuthenticationError, AuthorizationError) as exc:
            await interaction.followup.send(f"🔐 {exc}", ephemeral=True)
            return

        if status != 200 or not result.get("success"):
            await interaction.followup.send(
                "❌ No se pudo consultar el historial.", ephemeral=True
            )
            return

        transactions = result.get("transactions", [])
        saldo = result.get("balance", 0)

        if not transactions:
            await interaction.followup.send(
                "ℹ️ No hay movimientos que coincidan.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📊 Historial del gremio",
            description=f"Saldo actual: **{format_number(saldo)}** plata",
            color=Config.EMBED_COLOR,
        )

        for tx in transactions:
            tipo_tx = tx.get("type", "?")
            icon = {"ingreso": "➕", "egreso": "➖", "inicial": "🏁"}.get(tipo_tx, "•")
            sign = "-" if tipo_tx == "egreso" else "+"
            monto = format_number(tx.get("amount", 0))
            fecha = (tx.get("created_at") or "")[:10]
            reason = tx.get("reason", "—")[:60]
            by = tx.get("performed_by_name", "—")

            embed.add_field(
                name=f"{icon} {reason}",
                value=f"`{sign}{monto}` · por **{by}** · `{fecha}`",
                inline=False,
            )

        embed.set_footer(
            text=f"{Config.GUILD_NAME} · {len(transactions)} movimiento(s)"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ================================================================== #
    #                          Error handler                              #
    # ================================================================== #

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        logger.error("Error en BankCog: %s", error, exc_info=error)
        msg = "❌ Ocurrió un error inesperado. Intenta de nuevo."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog(bot))