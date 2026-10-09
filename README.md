# 🤖 Linhir Assistant Bot

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.4.0-blue.svg)](https://github.com/Rapptz/discord.py)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Bot oficial de **Linhir** en Albion Online. Gestiona la identidad de los
miembros del gremio, verifica la integridad del roster, envía notificaciones
masivas y expone utilidades del juego directamente en Discord.

Autenticado vía **Sanctum** contra la API privada de
[linhir.online](https://linhir.online).

---

## 📋 Tabla de contenidos

- [Características](#-características)
- [Comandos disponibles](#-comandos-disponibles)
- [Requisitos](#-requisitos)
- [Instalación local](#-instalación-local)
- [Configuración (.env)](#-configuración-env)
- [Ejecución](#-ejecución)
- [Despliegue en producción](#-despliegue-en-producción)
- [Estructura del proyecto](#-estructura-del-proyecto)
- [Solución de problemas](#-solución-de-problemas)
- [Flujo de Git](#-flujo-de-git)
- [Licencia](#-licencia)

---

## ✨ Características

- ✅ **Comandos slash** nativos de Discord (`/`).
- ✅ **Autenticación Sanctum** con token de cuenta bot dedicada.
- ✅ **Registro de identidad** Albion ↔ Discord (con verificación contra la API de Albion).
- ✅ **Sincronización bidireccional** de roles (`@Linhir`, `@Publico`) y nicknames.
- ✅ **Reporte de integridad diario** al canal de oficiales.
- ✅ **Detección de retiros** del gremio con confirmación por botones.
- ✅ **Recordatorios masivos** por DM con barra de progreso en vivo.
- ✅ **Anuncios por rol** con menciones controladas.
- ✅ **Sesión HTTP persistente** con reintentos exponenciales.
- ✅ **Logging estructurado** con rotación diaria.

---

## 🎮 Comandos disponibles

| Comando | Subcomando | Descripción | Permisos |
|---|---|---|---|
| `/hora` | — | Hora actual del servidor Albion y zonas hispanohablantes | Todos |
| `/oro` | — | Precio actual del oro en Albion West | Todos |
| `/register` | `start` | Vincula tu personaje con tu cuenta de Discord | Todos |
| `/register` | `manager` | Registra a otro miembro | Oficiales |
| `/register` | `update` | Sincroniza roles según estado actual en Albion | Todos / Oficiales (a otros) |
| `/report` | — | Genera el reporte de integridad de miembros | Oficiales |
| `/recordar` | — | Envía un DM a todos los miembros con un rol | Oficiales |
| `/anuncio` | — | Menciona a un rol en un canal con un mensaje | Oficiales |

> **Nota**: "Oficiales" incluye administradores nativos de Discord **y** cualquier
> rol cuyo ID esté listado en `OFFICER_ROLE_IDS` del `.env`.

---

## 🛠️ Requisitos

### Runtime
- **Python** 3.10 o superior
- **pip** y **venv**
- Acceso a Internet (API de Albion Online + API de Linhir)
- Bot de Discord con los siguientes **privileged intents** activados:
  - ✅ **SERVER MEMBERS INTENT** (obligatorio para `/report`)

