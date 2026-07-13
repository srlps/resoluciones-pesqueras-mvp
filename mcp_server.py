"""Servidor FastMCP: expone las herramientas de base de datos al agente extractor.

Análogo a `mcp_server.py` de la estructura sugerida por el profesor, con las 5 tools
del dominio (Ficha 3, sección 1.3 del PoC) en vez de las "una a tres" genéricas:
lectura (`ejecutar_consulta_sql`) + escritura atómica (`crear_norma`, `actualizar_norma`,
`derogar_norma`) + ruta de incertidumbre (`enviar_a_dlq`).

Ejecutar con: python mcp_server.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import text

import config
from database import engine, init_db

mcp = FastMCP("pesca-normas-mcp")


def _get_or_create_documento(conn, nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo):
    """Inserta el documento si no existe; siempre devuelve su id (idempotente por hash_pdf)."""
    conn.execute(
        text(
            """
            INSERT INTO documentos (nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo)
            VALUES (:nro, :hash, :url, :fecha, :tipo)
            ON CONFLICT (hash_pdf) DO NOTHING
            """
        ),
        {
            "nro": nro_resolucion,
            "hash": hash_pdf,
            "url": url_fuente,
            "fecha": fecha_publicacion or None,
            "tipo": tipo,
        },
    )
    row = conn.execute(
        text("SELECT id FROM documentos WHERE hash_pdf=:hash"), {"hash": hash_pdf}
    ).fetchone()
    return row[0]


@mcp.tool
def ejecutar_consulta_sql(query: str) -> str:
    """Ejecuta una consulta SQL de SOLO LECTURA sobre la base de datos normativa de PRODUCE.

    Úsala ANTES de cualquier escritura para: (1) detectar normas vigentes del mismo
    objeto/zona que puedan ser actualizadas o derogadas, (2) obtener el norma_id
    necesario para actualizar_norma y derogar_norma. Solo SELECT o WITH.

    Esquema: documentos(id, nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo);
    normas_actuales(id, actores, objeto, accion, lugar, vigencia_inicio, vigencia_fin,
    estado, datos_dinamicos JSONB); linea_tiempo_normas(id, norma_id, documento_id,
    fecha_cambio, tipo_cambio, descripcion).
    """
    stmt = query.strip().upper().lstrip("(")
    if not (stmt.startswith("SELECT") or stmt.startswith("WITH")):
        return "ERROR: solo se permiten SELECT o WITH."
    with engine.connect() as conn:
        try:
            rows = conn.execute(text(query)).fetchall()
            return str(rows) if rows else "Sin resultados para esta consulta."
        except Exception as e:  # noqa: BLE001 — se reporta al agente, no se relanza
            return f"Error en consulta: {e}"


@mcp.tool
def crear_norma(
    nro_resolucion: str,
    hash_pdf: str,
    actores: str,
    objeto: str,
    accion: str,
    lugar: str,
    vigencia_inicio: str,
    descripcion: str,
    datos_dinamicos_json: str = "{}",
    confianza: str = "alta",
    vigencia_fin: str = "",
    url_fuente: str = "",
    fecha_publicacion: str = "",
    tipo: str = "resolucion",
) -> str:
    """Flujo CREACIÓN: registra una norma nueva sin antecedente vigente en la BD.

    Inserta el documento (idempotente por hash_pdf), la norma en normas_actuales con
    estado='vigente', y una entrada 'crea' en linea_tiempo_normas. Solo llamar si
    confianza='alta' o 'media'; para 'baja' usar enviar_a_dlq.
    """
    try:
        datos = json.loads(datos_dinamicos_json) if datos_dinamicos_json else {}
    except json.JSONDecodeError:
        datos = {"raw": datos_dinamicos_json}

    with engine.begin() as conn:
        doc_id = _get_or_create_documento(conn, nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo)
        norma_row = conn.execute(
            text(
                """
                INSERT INTO normas_actuales
                    (actores, objeto, accion, lugar, vigencia_inicio, vigencia_fin, estado, datos_dinamicos)
                VALUES (:actores, :objeto, :accion, :lugar, :v_ini, :v_fin, 'vigente', CAST(:datos AS jsonb))
                RETURNING id
                """
            ),
            {
                "actores": actores,
                "objeto": objeto,
                "accion": accion,
                "lugar": lugar,
                "v_ini": vigencia_inicio,
                "v_fin": vigencia_fin or None,
                "datos": json.dumps(datos),
            },
        ).fetchone()
        norma_id = norma_row[0]
        conn.execute(
            text(
                """
                INSERT INTO linea_tiempo_normas (norma_id, documento_id, fecha_cambio, tipo_cambio, descripcion)
                VALUES (:norma, :doc, CURRENT_DATE, 'crea', :desc)
                """
            ),
            {"norma": norma_id, "doc": doc_id, "desc": descripcion},
        )

    return f"Norma creada: norma_id={norma_id}, documento_id={doc_id}, confianza={confianza}."


@mcp.tool
def actualizar_norma(
    norma_id: int,
    nro_resolucion: str,
    hash_pdf: str,
    descripcion: str,
    actores: str = "",
    objeto: str = "",
    accion: str = "",
    lugar: str = "",
    vigencia_inicio: str = "",
    vigencia_fin: str = "",
    datos_dinamicos_json: str = "",
    url_fuente: str = "",
    fecha_publicacion: str = "",
    tipo: str = "resolucion",
) -> str:
    """Flujo ACTUALIZACIÓN: modifica campos de una norma vigente existente (cuota, zona, fechas...).

    Solo pasa los campos que efectivamente cambian; los demás quedan intactos.
    norma_id se obtiene con ejecutar_consulta_sql.
    """
    with engine.begin() as conn:
        doc_id = _get_or_create_documento(conn, nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo)

        campos: dict = {}
        if actores:
            campos["actores"] = actores
        if objeto:
            campos["objeto"] = objeto
        if accion:
            campos["accion"] = accion
        if lugar:
            campos["lugar"] = lugar
        if vigencia_inicio:
            campos["vigencia_inicio"] = vigencia_inicio
        if vigencia_fin:
            campos["vigencia_fin"] = vigencia_fin

        set_parts = [f"{k}=:{k}" for k in campos]

        if datos_dinamicos_json:
            try:
                datos = json.loads(datos_dinamicos_json)
            except json.JSONDecodeError:
                datos = {"raw": datos_dinamicos_json}
            campos["datos_dinamicos"] = json.dumps(datos)
            set_parts.append("datos_dinamicos=CAST(:datos_dinamicos AS jsonb)")

        if set_parts:
            campos["norma_id"] = norma_id
            conn.execute(text(f"UPDATE normas_actuales SET {', '.join(set_parts)} WHERE id=:norma_id"), campos)

        conn.execute(
            text(
                """
                INSERT INTO linea_tiempo_normas (norma_id, documento_id, fecha_cambio, tipo_cambio, descripcion)
                VALUES (:norma, :doc, CURRENT_DATE, 'actualiza', :desc)
                """
            ),
            {"norma": norma_id, "doc": doc_id, "desc": descripcion},
        )

    campos_actualizados = [k for k in campos if k != "norma_id"]
    return f"Norma {norma_id} actualizada. Campos modificados: {campos_actualizados}. documento_id={doc_id}."


@mcp.tool
def derogar_norma(
    norma_id: int,
    nro_resolucion: str,
    hash_pdf: str,
    descripcion: str,
    url_fuente: str = "",
    fecha_publicacion: str = "",
    tipo: str = "resolucion",
) -> str:
    """Flujo DEROGACIÓN: anula una norma vigente por disposición explícita de una resolución
    ("se deroga", "se deja sin efecto", "se levanta", etc.). norma_id se obtiene con
    ejecutar_consulta_sql.
    """
    with engine.begin() as conn:
        doc_id = _get_or_create_documento(conn, nro_resolucion, hash_pdf, url_fuente, fecha_publicacion, tipo)
        conn.execute(text("UPDATE normas_actuales SET estado='derogada' WHERE id=:id"), {"id": norma_id})
        conn.execute(
            text(
                """
                INSERT INTO linea_tiempo_normas (norma_id, documento_id, fecha_cambio, tipo_cambio, descripcion)
                VALUES (:norma, :doc, CURRENT_DATE, 'deroga', :desc)
                """
            ),
            {"norma": norma_id, "doc": doc_id, "desc": descripcion},
        )

    return f"Norma {norma_id} marcada como derogada. documento_id={doc_id}."


@mcp.tool
def enviar_a_dlq(motivo: str, hash_pdf: str = "", datos_parciales_json: str = "{}") -> str:
    """Desvía un documento a la Dead Letter Queue cuando la confianza de extracción es baja.

    Usar SIEMPRE que confianza='baja' o cuando objeto/accion no sean identificables en el texto.
    Queda disponible para revisión humana desde la interfaz HTML.
    """
    try:
        datos = json.loads(datos_parciales_json) if datos_parciales_json else {}
    except json.JSONDecodeError:
        datos = {"raw": datos_parciales_json}

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO dlq_documentos (hash_pdf, motivo, datos_parciales, fecha_creacion, revisado)
                VALUES (:hash, :motivo, CAST(:datos AS jsonb), :ts, FALSE)
                RETURNING id
                """
            ),
            {
                "hash": hash_pdf,
                "motivo": motivo,
                "datos": json.dumps(datos),
                "ts": datetime.now(timezone.utc),
            },
        ).fetchone()

    return f"Documento enviado a DLQ (entrada #{row[0]}). Motivo: {motivo}"


if __name__ == "__main__":
    init_db()
    mcp.run(transport="http", host=config.MCP_SERVER_HOST, port=config.MCP_SERVER_PORT)
