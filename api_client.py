import asyncio
import logging

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Token inválido, expirado o revocado (401)."""


class AuthorizationError(Exception):
    """Token válido pero sin ability suficiente (403)."""


class APIClient:
    """
    Cliente HTTP asíncrono con Sanctum Bearer auth,
    sesión persistente y reintentos exponenciales.
    """

    _session: aiohttp.ClientSession | None = None

    # ============================ Infraestructura ============================

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """Devuelve la sesión compartida, creándola si es necesario."""
        if cls._session is None or cls._session.closed:
            timeout = aiohttp.ClientTimeout(total=Config.API_TIMEOUT)
            cls._session = aiohttp.ClientSession(
                timeout=timeout,
                headers=Config.auth_headers(),
            )
        return cls._session

    @classmethod
    async def close(cls) -> None:
        """Cierra la sesión (llamar en shutdown del bot)."""
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    @classmethod
    async def _request(
        cls,
        method: str,
        path: str,
        data: dict | None = None,
        retries: int = 2,
    ) -> tuple[dict, int]:
        """
        Request autenticado con reintentos exponenciales.
        Soporta GET, POST, PUT, DELETE.

        Retorna siempre (dict, status_code). Nunca lanza excepción de red.
        Distingue 401 (auth) y 403 (permisos) para logging apropiado.
        """
        session = await cls.get_session()
        url = Config.api_url(path)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with session.request(method, url, json=data) as resp:
                    try:
                        body = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {"raw": await resp.text()}

                    if resp.status == 401:
                        logger.critical(
                            "🚨 Token Sanctum inválido/expirado para %s %s. "
                            "Regenera el token desde el panel de administración.",
                            method, path,
                        )
                        raise AuthenticationError(
                            "Token Sanctum inválido o expirado. "
                            "Contacta al administrador del bot."
                        )

                    if resp.status == 403:
                        logger.error(
                            "⛔ Token sin ability suficiente para %s %s: %s",
                            method, path, body,
                        )
                        raise AuthorizationError(
                            "El bot no tiene permisos para este comando. "
                            "Verifica las abilities del token."
                        )

                    # --- MANEJO DE ERRORES DE LARAVEL ---
                    if resp.status >= 400:
                        logger.warning("API %s %s → %s: %s", method, path, resp.status, body)
                        
                        # Error de validación (422 Unprocessable Entity)
                        if resp.status == 422 and isinstance(body, dict) and "errors" in body:
                            errors = body["errors"]
                            if errors:
                                first_error = list(errors.values())[0][0]
                                return {"error": first_error}, 422
                            return {"error": body.get("message", "Datos inválidos.")}, 422
                        
                        # Error interno del servidor (500)
                        if resp.status == 500:
                            return {"error": "Error interno en el servidor de Linhir. Revisa los logs."}, 500
                        
                        # Otros errores
                        return body, resp.status

                    return body, resp.status

            except (AuthenticationError, AuthorizationError):
                # No reintentar: son errores permanentes
                raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning(
                    "API %s %s intento %d/%d falló: %s",
                    method, path, attempt + 1, retries + 1, exc,
                )
                if attempt < retries:
                    await asyncio.sleep(1.5 ** attempt)

        return {"error": str(last_error)}, 500

    @classmethod
    async def _get(cls, path: str, retries: int = 2) -> tuple[dict, int]:
        """Atajo para GET."""
        return await cls._request("GET", path, None, retries)

    # ============================ Endpoints ============================

    @classmethod
    async def get_server_time(cls) -> tuple[dict, int]:
        """GET /api/bot/horario"""
        return await cls._get("/horario")

    @classmethod
    async def get_gold_price(cls) -> tuple[dict, int]:
        """GET /api/bot/oro"""
        return await cls._get("/oro")

    @classmethod
    async def sync_registration(cls, discord_user_id: str) -> tuple[dict, int]:
        """POST /api/bot/sync-registration"""
        payload = {"discord_user_id": discord_user_id}
        return await cls._request("POST", "/sync-registration", payload)

    @classmethod
    async def get_registered_members(cls) -> tuple[dict, int]:
        """GET /api/bot/members"""
        return await cls._get("/members")

    @classmethod
    async def search_character(cls, name: str) -> tuple[dict, int]:
        """POST /api/bot/search-character"""
        return await cls._request("POST", "/search-character", {"character_name": name})

    @classmethod
    async def register_character(
        cls,
        character_name: str | None = None,
        character_id: str | None = None,
        discord_user_id: str = "",
        birthdate: str | None = None,
        discord_username: str | None = None,
        performed_by_discord_id: str | None = None,
    ) -> tuple[dict, int]:
        """POST /api/bot/register-character"""
        payload: dict = {"discord_user_id": discord_user_id}
        if character_id:
            payload["character_id"] = character_id
        elif character_name:
            payload["character_name"] = character_name
        if birthdate:
            payload["birthdate"] = birthdate
        if discord_username:
            payload["discord_username"] = discord_username
        if performed_by_discord_id:
            payload["performed_by_discord_id"] = performed_by_discord_id
        return await cls._request("POST", "/register-character", payload)

    @classmethod
    async def get_my_balance(cls, discord_user_id: str) -> tuple[dict, int]:
        return await cls._get(f"/members/{discord_user_id}/balance")

    @classmethod
    async def create_withdrawal(cls, discord_user_id: str, amount: int) -> tuple[dict, int]:
        return await cls._request("POST", "/withdrawals", {"discord_user_id": discord_user_id, "amount": int(amount)})

    @classmethod
    async def get_my_withdrawals(cls, discord_user_id: str, limit: int = 10) -> tuple[dict, int]:
        return await cls._get(f"/withdrawals/mine?discord_user_id={discord_user_id}&limit={int(limit)}")

    @classmethod
    async def get_pending_withdrawals(cls) -> tuple[dict, int]:
        return await cls._get("/withdrawals/pending")

    @classmethod
    async def approve_withdrawal(cls, withdrawal_id: int, reviewer_discord_id: str, reviewer_name: str) -> tuple[dict, int]:
        payload = {"reviewer_discord_id": reviewer_discord_id, "reviewer_name": reviewer_name}
        return await cls._request("POST", f"/withdrawals/{withdrawal_id}/approve", payload)

    @classmethod
    async def reject_withdrawal(cls, withdrawal_id: int, reviewer_discord_id: str, reviewer_name: str, reason: str) -> tuple[dict, int]:
        payload = {"reviewer_discord_id": reviewer_discord_id, "reviewer_name": reviewer_name, "reason": reason[:255]}
        return await cls._request("POST", f"/withdrawals/{withdrawal_id}/reject", payload)

    @classmethod
    async def get_guild_balance(cls) -> tuple[dict, int]:
        return await cls._get("/guild-balance")

    @classmethod
    async def get_guild_transactions(cls, limit: int = 15, offset: int = 0, tipo: str | None = None) -> tuple[dict, int]:
        path = f"/guild-transactions?limit={int(limit)}&offset={int(offset)}"
        if tipo:
            path += f"&type={tipo}"
        return await cls._get(path)

    @classmethod
    async def create_guild_transaction(cls, tipo: str, amount: int, reason: str, performed_by_discord_id: str, performed_by_name: str, image_url: str | None = None) -> tuple[dict, int]:
        payload = {"type": tipo, "amount": int(amount), "reason": reason[:255], "performed_by_discord_id": performed_by_discord_id, "performed_by_name": performed_by_name}
        if image_url:
            payload["image_url"] = image_url
        return await cls._request("POST", "/guild-transactions", payload)

    @classmethod
    async def create_activity(cls, payload: dict) -> tuple[dict, int]:
        return await cls._request("POST", "/activities", payload)

    @classmethod
    async def get_activities(cls, limit: int = 20, offset: int = 0) -> tuple[dict, int]:
        return await cls._get(f"/activities?limit={int(limit)}&offset={int(offset)}")

    @classmethod
    async def get_activity(cls, activity_id: int) -> tuple[dict, int]:
        return await cls._get(f"/activities/{int(activity_id)}")

    @classmethod
    async def get_compositions(cls) -> tuple[dict, int]:
        return await cls._get("/compositions")

    @classmethod
    async def create_composition(cls, name: str, description: str | None, weapons: list[dict]) -> tuple[dict, int]:
        payload = {"name": name[:120], "weapons": [{"name": w["name"][:120], "icon": (w.get("icon") or None)} for w in weapons]}
        if description:
            payload["description"] = description[:500]
        return await cls._request("POST", "/compositions", payload)

    @classmethod
    async def delete_composition(cls, composition_id: int) -> tuple[dict, int]:
        return await cls._request("DELETE", f"/compositions/{int(composition_id)}")

    @classmethod
    async def get_active_ctas(cls) -> tuple[dict, int]:
        return await cls._get("/ctas/active")

    @classmethod
    async def get_cta(cls, cta_id: int) -> tuple[dict, int]:
        return await cls._get(f"/ctas/{int(cta_id)}")

    @classmethod
    async def create_cta(cls, payload: dict) -> tuple[dict, int]:
        return await cls._request("POST", "/ctas", payload)

    @classmethod
    async def cta_join(cls, cta_id: int, discord_user_id: str, user_name: str, weapon_id: int) -> tuple[dict, int]:
        payload = {"discord_user_id": discord_user_id, "user_name": user_name, "weapon_id": int(weapon_id)}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/join", payload)

    @classmethod
    async def cta_absent(cls, cta_id: int, discord_user_id: str) -> tuple[dict, int]:
        payload = {"discord_user_id": discord_user_id}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/absent", payload)

    @classmethod
    async def cta_cancel(cls, cta_id: int) -> tuple[dict, int]:
        return await cls._request("POST", f"/ctas/{int(cta_id)}/cancel")

    @classmethod
    async def get_pending_reminders(cls) -> tuple[dict, int]:
        return await cls._get("/ctas/pending-reminders")

    @classmethod
    async def mark_reminder_sent(cls, cta_id: int) -> tuple[dict, int]:
        return await cls._request("POST", f"/ctas/{int(cta_id)}/reminder-sent")

    @classmethod
    async def get_ctas(cls, status: str | None = None, paid: bool | None = None, limit: int = 20, offset: int = 0, order: str = "desc") -> tuple[dict, int]:
        params = [f"limit={int(limit)}", f"offset={int(offset)}", f"order={order}"]
        if status:
            params.append(f"status={status}")
        if paid is not None:
            params.append(f"paid={'true' if paid else 'false'}")
        path = "/ctas?" + "&".join(params)
        return await cls._get(path)

    @classmethod
    async def get_pending_payment_ctas(cls) -> tuple[dict, int]:
        return await cls._get("/ctas/pending-payment")
    
    @classmethod
    async def get_allied_guilds(cls) -> tuple[dict, int]:
        return await cls._get("/allied-guilds")
    
    @classmethod
    async def get_pending_announcements(cls) -> tuple[dict, int]:
        return await cls._get("/ctas/pending-announcements")

    @classmethod
    async def mark_announced(cls, cta_id: int, message_id: str, channel_id: str) -> tuple[dict, int]:
        payload = {"discord_message_id": message_id, "discord_channel_id": channel_id}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/mark-announced", payload)

    @classmethod
    async def update_voice_channel(cls, cta_id: int, voice_channel_id: str) -> tuple[dict, int]:
        payload = {"discord_voice_channel_id": voice_channel_id}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/voice-channel", payload)

    @classmethod
    async def add_voice_time(cls, cta_id: int, discord_user_id: str, seconds: int) -> tuple[dict, int]:
        payload = {"discord_user_id": discord_user_id, "seconds": seconds}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/voice-time", payload)
    
    @classmethod
    async def update_cta_announce(cls, cta_id: int, announce_at: str) -> tuple[dict, int]:
        payload = {"announce_at": announce_at}
        return await cls._request("POST", f"/ctas/{int(cta_id)}/announce", payload)
    
    @classmethod
    async def get_ctas_with_voice(cls) -> tuple[dict, int]:
        """GET /api/bot/ctas/voice-channels"""
        return await cls._get("/ctas/voice-channels")

    @classmethod
    async def clear_voice_channel(cls, cta_id: int) -> tuple[dict, int]:
        """POST /api/bot/ctas/{id}/clear-voice-channel"""
        return await cls._request("POST", f"/ctas/{int(cta_id)}/clear-voice-channel")