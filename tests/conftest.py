"""Fixtures compartidas por los tests. En particular, `mcp_de_prueba` levanta un
servidor MCP real (el mismo `mcp_server.py` del proyecto) contra una base de datos
PostgreSQL descartable, para que los casos de aceptación que ejercitan la cascada
completa (ver `test_casos_aceptacion.py`) no dependan de tener `python mcp_server.py`
corriendo a mano en otra terminal, ni de escribir sobre la BD de desarrollo.

Por qué no "en memoria" de verdad: el esquema (`database.py`) usa JSONB, SERIAL,
`CAST(:datos AS jsonb)` y `ON CONFLICT` — sintaxis específica de PostgreSQL que la
única opción realmente "en memoria" en Python (SQLite `:memory:`) no soporta sin
reescribir el SQL de producción solo para los tests (justo lo que se quiere evitar).
La alternativa equivalente en la práctica es una base Postgres descartable: se
recrea desde cero (`DROP` + `CREATE`) al iniciar la sesión de pytest, vive en el
mismo servidor Postgres que ya tienes corriendo localmente (mismo host/usuario de
tu `.env`, solo cambia el nombre de la BD a `<db>_test`), y nunca comparte datos con
`DATABASE_URL`. Requiere tener un servidor PostgreSQL alcanzable; si no lo hay, la
fixture hace `pytest.skip(...)` en vez de fallar.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

import config

TEST_MCP_PORT = 8766
TEST_MCP_HOST = "127.0.0.1"

_URL_ORIGINAL = make_url(config.DATABASE_URL)
_NOMBRE_BD_PRUEBA = f"{_URL_ORIGINAL.database}_test"
_URL_MANTENIMIENTO = _URL_ORIGINAL.set(database="postgres")
_URL_PRUEBA = _URL_ORIGINAL.set(database=_NOMBRE_BD_PRUEBA)


def _postgres_disponible() -> bool:
    try:
        motor = create_engine(_URL_MANTENIMIENTO)
        with motor.connect() as conn:
            conn.execute(text("SELECT 1"))
        motor.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def _puerto_escuchando(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _terminar_conexiones(conn) -> None:
    """Corta cualquier conexión abierta a la BD de prueba antes de un DROP/CREATE
    (necesario porque Postgres no permite dropear una BD con sesiones activas)."""
    conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db AND pid <> pg_backend_pid()"
        ),
        {"db": _NOMBRE_BD_PRUEBA},
    )


def _recrear_bd_prueba() -> None:
    """DROP + CREATE de la BD de prueba: cada sesión de pytest arranca desde cero."""
    motor = create_engine(_URL_MANTENIMIENTO, isolation_level="AUTOCOMMIT")
    with motor.connect() as conn:
        _terminar_conexiones(conn)
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_NOMBRE_BD_PRUEBA}"'))
        conn.execute(text(f'CREATE DATABASE "{_NOMBRE_BD_PRUEBA}"'))
    motor.dispose()


def _eliminar_bd_prueba() -> None:
    motor = create_engine(_URL_MANTENIMIENTO, isolation_level="AUTOCOMMIT")
    with motor.connect() as conn:
        _terminar_conexiones(conn)
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_NOMBRE_BD_PRUEBA}"'))
    motor.dispose()


@pytest.fixture(scope="session")
def event_loop_compartido():
    """Un único event loop de asyncio para toda la sesión de pytest.

    `agent.py` instancia `gemini_flash`/`mistral_large` (y sus clientes httpx
    async internos) como singletons de módulo. En Windows (`ProactorEventLoop`),
    reutilizar esos clientes después de que un `asyncio.run()` anterior cerró su
    loop revienta con `RuntimeError: Event loop is closed` al reciclar la conexión
    keep-alive. Correr todas las llamadas async de los tests sobre el MISMO loop
    evita el problema — igual que en producción, donde uvicorn mantiene un único
    loop para toda la vida del proceso en vez de crear uno por request.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def mcp_de_prueba():
    """Levanta `mcp_server.py` en un hilo de fondo (puerto 8766, distinto del real
    8765) apuntando a la BD de prueba, y redirige `agent.py` para que le hable a
    ese servidor en vez de al que esté configurado en `MCP_SERVER_URL`.

    Se salta automáticamente si no hay un servidor PostgreSQL alcanzable.
    Yields el engine de SQLAlchemy de la BD de prueba, para que el propio test
    pueda verificar directamente lo que el agente persistió.
    """
    if not _postgres_disponible():
        pytest.skip(
            "Requiere un servidor PostgreSQL alcanzable (mismo host/credenciales "
            "que DATABASE_URL) para crear la BD de prueba descartable."
        )

    _recrear_bd_prueba()

    # Imports diferidos: deben ocurrir después de decidir si se salta el fixture,
    # para no pagar el costo de construir los clientes LLM/MCP en balde.
    import agent as agent_module
    import database as database_module
    import mcp_server as mcp_server_module
    from langchain_mcp_adapters.client import MultiServerMCPClient

    motor_prueba = create_engine(_URL_PRUEBA, pool_pre_ping=True)

    # database.init_db() usa el `engine` global del módulo — se apunta temporalmente
    # a la BD de prueba para crear ahí el esquema (documentos/normas/línea de tiempo/DLQ).
    motor_original_database = database_module.engine
    database_module.engine = motor_prueba
    try:
        database_module.init_db()
    finally:
        database_module.engine = motor_original_database

    # Las tools de mcp_server.py y la consulta de deduplicación de agent.py usan
    # cada una su propio `engine` importado en tiempo de import — se redirigen los
    # dos a la BD de prueba (el `engine` de la app real / dev, sin tocar).
    motor_original_mcp = mcp_server_module.engine
    motor_original_agent = agent_module.engine
    mcp_server_module.engine = motor_prueba
    agent_module.engine = motor_prueba

    def _lanzar_servidor() -> None:
        asyncio.run(
            mcp_server_module.mcp.run_async(
                transport="http", host=TEST_MCP_HOST, port=TEST_MCP_PORT, show_banner=False
            )
        )

    hilo = threading.Thread(target=_lanzar_servidor, daemon=True)
    hilo.start()

    for _ in range(50):  # ~5s de margen para que uvicorn levante en el hilo
        if _puerto_escuchando(TEST_MCP_HOST, TEST_MCP_PORT):
            break
        time.sleep(0.1)
    else:
        pytest.skip("El servidor MCP de prueba no respondió a tiempo en el puerto de test.")

    cliente_original = agent_module._mcp_client
    agent_module._mcp_client = MultiServerMCPClient(
        {
            "pesca_normas": {
                "url": f"http://{TEST_MCP_HOST}:{TEST_MCP_PORT}/mcp",
                "transport": "streamable_http",
            }
        }
    )
    agent_module._agente_extractor = None  # se reconstruye contra el cliente de prueba

    try:
        yield motor_prueba
    finally:
        agent_module._mcp_client = cliente_original
        agent_module._agente_extractor = None
        agent_module.engine = motor_original_agent
        mcp_server_module.engine = motor_original_mcp
        motor_prueba.dispose()
        _eliminar_bd_prueba()
