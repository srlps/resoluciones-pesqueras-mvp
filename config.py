"""Variables de entorno y configuración mínima del MVP.

Todo módulo que necesite una clave, URL o nombre de modelo la importa desde aquí,
en vez de leer `os.environ` directamente. Facilita testear con mocks.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # lee .env si existe (nunca se commitea; ver .env.example)

# ── Modelos de la cascada (Ficha 2) ──────────────────────────────────────────
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")

# ── Observabilidad ────────────────────────────────────────────────────────────
LANGSMITH_TRACING = os.environ.get("LANGSMITH_TRACING", "false")
LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "fishing-rules-updates-mvp")
LANGSMITH_API_KEY = os.environ.get("LANGSMITH_API_KEY", "")

if LANGSMITH_TRACING.lower() == "true":
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
    if LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY

# ── Persistencia (PostgreSQL externo — capa Staging/Curated) ────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/pesca_mvp"
)

# ── Servidor MCP ──────────────────────────────────────────────────────────────
MCP_SERVER_HOST = os.environ.get("MCP_SERVER_HOST", "127.0.0.1")
MCP_SERVER_PORT = int(os.environ.get("MCP_SERVER_PORT", "8765"))
MCP_SERVER_URL = os.environ.get(
    "MCP_SERVER_URL", f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}/mcp"
)

# ── App web (FastAPI + HTML estático) ─────────────────────────────────────────
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))
