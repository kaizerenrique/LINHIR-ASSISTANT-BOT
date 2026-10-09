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

                    if resp.status >= 400:
                        logger.warning("API %s %s → %s: %s", method, path, resp.status, body)

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
        """
        GET /api/bot/horario
        Retorna: {"timestamp": int, "utc": "...", "venezuela": "...", ...}
        """
        return await cls._get("/horario")

    @classmethod
    async def get_gold_price(cls) -> tuple[dict, int]:
        """
        GET /api/bot/oro
        Retorna: {"price": int, "timestamp": "Y-m-d H:i:s"}
        """
        return await cls._get("/oro")

    @classmethod
    async def register_character(
        cls,
        character_name: str,
        discord_user_id: str,
        birthdate: str | None = None,
        discord_username: str | None = None,
        performed_by_discord_id: str | None = None,
    ) -> tuple[dict, int]:
        """
        POST /api/bot/register-character
        Registra un personaje. Si `performed_by_discord_id` viene, se guarda en logs
        para auditoría (caso de un oficial registrando a otro).
        """
        payload: dict = {
            "character_name": character_name,
            "discord_user_id": discord_user_id,
        }
        if birthdate:
            payload["birthdate"] = birthdate
        if discord_username:
            payload["discord_username"] = discord_username
        if performed_by_discord_id:
            payload["performed_by_discord_id"] = performed_by_discord_id

        return await cls._request("POST", "/register-character", payload)
    
    @classmethod
    async def sync_registration(cls, discord_user_id: str) -> tuple[dict, int]:
        """
        POST /api/bot/sync-registration
        Re-consulta Albion y devuelve si el personaje sigue siendo miembro del gremio.
        """
        payload = {"discord_user_id": discord_user_id}
        return await cls._request("POST", "/sync-registration", payload)
    
    @classmethod
    async def get_registered_members(cls) -> tuple[dict, int]:
        """
        GET /api/bot/members
        Devuelve el roster completo del gremio con estado de vinculación Discord.
        Estructura: {success, guild_id, totals, guild_registered, guild_unregistered, all_linked}
        """
        return await cls._get("/members")