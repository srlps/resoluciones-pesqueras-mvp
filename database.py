"""Conexión a PostgreSQL externo y creación idempotente del esquema Medallion (capa Curated).

Un único `engine` compartido por `mcp_server.py` (tools de lectura/escritura) y por
`app.py` (timeline / DLQ para la interfaz HTML).
"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import config

engine: Engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)


def init_db() -> None:
    """Crea las tablas si no existen. Seguro de llamar múltiples veces (idempotente)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS documentos (
                id                SERIAL PRIMARY KEY,
                nro_resolucion    TEXT NOT NULL,
                hash_pdf          TEXT UNIQUE NOT NULL,
                url_fuente        TEXT,
                fecha_publicacion DATE,
                tipo              TEXT DEFAULT 'resolucion'
            )
            """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS normas_actuales (
                id              SERIAL PRIMARY KEY,
                actores         TEXT,
                objeto          TEXT NOT NULL,
                accion          TEXT CHECK (accion IN
                                    ('veda','cuota','permiso','prohibicion','otro')),
                lugar           TEXT,
                vigencia_inicio DATE,
                vigencia_fin    DATE,
                estado          TEXT DEFAULT 'vigente'
                                    CHECK (estado IN ('vigente','expirada','derogada')),
                datos_dinamicos JSONB DEFAULT '{}'
            )
            """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS linea_tiempo_normas (
                id           SERIAL PRIMARY KEY,
                norma_id     INTEGER REFERENCES normas_actuales(id),
                documento_id INTEGER REFERENCES documentos(id),
                fecha_cambio DATE DEFAULT CURRENT_DATE,
                tipo_cambio  TEXT,   -- 'crea' | 'actualiza' | 'deroga' | 'expira'
                descripcion  TEXT
            )
            """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS dlq_documentos (
                id                SERIAL PRIMARY KEY,
                hash_pdf          TEXT,
                motivo            TEXT,
                datos_parciales   JSONB DEFAULT '{}',
                fecha_creacion    TIMESTAMP DEFAULT NOW(),
                revisado          BOOLEAN DEFAULT FALSE
            )
            """
            )
        )


if __name__ == "__main__":
    init_db()
    print("Esquema verificado: documentos | normas_actuales | linea_tiempo_normas | dlq_documentos")
