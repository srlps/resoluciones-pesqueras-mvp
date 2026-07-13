"""Servidor FastMCP: expone las herramientas de base de datos al agente extractor.

Análogo a `mcp_server.py` de la estructura sugerida por el profesor, con las tools
del dominio (Ficha 3, sección 1.3 del PoC) en vez de las "una a tres" genéricas:
lectura estructurada por filtros (`buscar_normas`), lectura puntual por ID
(`obtener_norma`) + escritura atómica (`crear_norma`, `actualizar_norma`,
`derogar_norma`) + ruta de incertidumbre (`enviar_a_dlq`).

Ejecutar con: python mcp_server.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import config
from database import engine, init_db

mcp = FastMCP(
    name="pesca-normas-mcp",
    instructions="""
    Persistencia de normas pesqueras de PRODUCE (Perú): documentos, normas vigentes
    y su línea de tiempo de cambios (crea/actualiza/deroga/expira).

    MAPA DE HERRAMIENTAS (usa la mínima suficiente):
    • buscar_normas — Punto de entrada para descubrir normas. Solo lectura;
      filtra por objeto, accion, lugar, actores, estado o ventana de vigencia
      (vigente_desde/vigente_hasta) y devuelve, por cada norma encontrada, su
      línea de tiempo completa de documentos.
    • buscar_normas_por_resolucion — Alternativa a buscar_normas cuando el texto
      referencia OTRA resolución por su número (ej. "se deroga la norma de la
      R.M. 234-2025-PRODUCE"). Encuentra directamente las normas que ese
      documento afectó, sin adivinar objeto/zona.
    • obtener_norma — Ficha completa de UNA norma ya identificada por norma_id
      (obtenido con buscar_normas o buscar_normas_por_resolucion), con su línea
      de tiempo completa.
    • crear_norma       — norma nueva sin antecedente vigente en la BD.
    • actualizar_norma  — modifica campos de una norma vigente existente (requiere
      norma_id obtenido con buscar_normas o buscar_normas_por_resolucion).
    • derogar_norma     — anula una norma vigente por disposición explícita
      (requiere norma_id obtenido con buscar_normas o buscar_normas_por_resolucion).
    • enviar_a_dlq      — SIEMPRE que confianza='baja' o el objeto/acción de la
      norma no sean identificables en el texto. No llamar otras tools de escritura
      en ese caso.

    RESOURCE de referencia (consúltalo si no conoces los valores permitidos):
    pesca://schema — enum de `accion`, estados válidos y forma de `datos_dinamicos`.

    REGLA GENERAL DE ENCADENAMIENTO:
    1) buscar_normas para detectar normas vigentes del mismo objeto/zona
       y obtener el norma_id si existe antecedente. Si el texto menciona el
       número de OTRA resolución en vez de describir el objeto/zona, usa
       buscar_normas_por_resolucion en su lugar. Usa obtener_norma en vez de
       buscar_normas cuando ya conoces el norma_id exacto (ej. para reconfirmar
       su estado tras una escritura previa).
    2) Según lo encontrado: crear_norma (sin antecedente), actualizar_norma
       (antecedente que cambia parámetros) o derogar_norma (antecedente anulado
       explícitamente). Una resolución puede requerir derogar_norma + crear_norma
       en la misma llamada si deroga una norma y crea otra a la vez.
    3) Si en cualquier punto el objeto o la acción no son identificables con
       certeza en el texto del documento: enviar_a_dlq y detener el flujo.

    POLÍTICA DE INCERTIDUMBRE ("nunca inventar"): los campos que se escriben deben
    salir exclusivamente del texto del documento que se está procesando, nunca de
    valores leídos de la BD ni de suposiciones. Ante la duda, enviar_a_dlq.
    """,
)


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


ACCIONES_VALIDAS = {"veda", "cuota", "permiso", "prohibicion", "otro"}
ESTADOS_VALIDOS = {"vigente", "expirada", "derogada"}

_QUERY_HISTORIAL = text(
    """
    SELECT lt.fecha_cambio, lt.tipo_cambio, lt.descripcion,
           d.nro_resolucion, d.url_fuente
    FROM linea_tiempo_normas lt
    LEFT JOIN documentos d ON d.id = lt.documento_id
    WHERE lt.norma_id = :norma_id
    ORDER BY lt.fecha_cambio ASC, lt.id ASC
    """
)


def _formatear_norma_con_historial(conn, norma) -> str:
    """Construye el bloque de texto de una norma (campos + línea de tiempo).

    Compartido por `buscar_normas` y `obtener_norma` para no duplicar el formato.
    """
    bloque = [
        f"norma_id={norma['id']}  |  estado={norma['estado']}",
        f"  actores: {norma['actores']}",
        f"  objeto: {norma['objeto']}",
        f"  accion: {norma['accion']}",
        f"  lugar: {norma['lugar']}",
        f"  vigencia: {norma['vigencia_inicio']} a {norma['vigencia_fin'] or 'indefinida'}",
        f"  datos_dinamicos: {norma['datos_dinamicos']}",
    ]

    historial = conn.execute(_QUERY_HISTORIAL, {"norma_id": norma["id"]}).mappings().all()
    if historial:
        bloque.append("  Línea de tiempo:")
        for h in historial:
            bloque.append(
                f"    [{h['fecha_cambio']}] {h['tipo_cambio']} — "
                f"{h['nro_resolucion'] or 'N/D'}: {h['descripcion']}"
            )
    else:
        bloque.append("  Línea de tiempo: sin registros.")

    return "\n".join(bloque)


@mcp.tool(
    title="Buscar normas por filtros",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def buscar_normas(
    objeto: Optional[str] = None,
    accion: Optional[str] = None,
    lugar: Optional[str] = None,
    actores: Optional[str] = None,
    estado: Optional[str] = None,
    vigente_desde: Optional[str] = None,
    vigente_hasta: Optional[str] = None,
    limite: int = 10,
) -> str:
    """Busca normas por filtros combinables y devuelve, por cada una, su línea de
    tiempo completa (qué documentos la crearon, actualizaron o derogaron).

    Cuándo usar:
    • Punto de entrada para descubrir normas: SIEMPRE antes de crear_norma,
      actualizar_norma o derogar_norma, para detectar si ya existe una norma
      relacionada y obtener el norma_id que esas tools necesitan. Combina los
      filtros que tengas disponibles (objeto, accion, lugar, actores, estado,
      ventana de vigencia); no es necesario conocerlos todos ni usarlos todos.

    Cuándo NO usar:
    • Ya conoces el norma_id exacto y solo quieres su ficha/historial → usa
      obtener_norma (más directo, no depende de que los filtros coincidan).
    • El texto referencia otra resolución por su número → usa
      buscar_normas_por_resolucion en su lugar.
    • Para escribir o modificar datos → usa crear_norma / actualizar_norma /
      derogar_norma / enviar_a_dlq.

    Todos los filtros son opcionales y combinables con AND; `objeto`, `lugar` y
    `actores` hacen coincidencia parcial insensible a mayúsculas (ej. objeto="anchoveta"
    encuentra "anchoveta (Engraulis ringens)"). Sin filtros, devuelve las normas
    más recientes según `limite`.

    Args:
        objeto: Texto parcial de la especie o recurso. Ej: "anchoveta", "merluza".
        accion: Uno de 'veda' | 'cuota' | 'permiso' | 'prohibicion' | 'otro'.
        lugar: Texto parcial del ámbito geográfico. Ej: "norte-centro", "litoral sur".
        actores: Texto parcial de las personas/entidades/embarcaciones sujetas a la
            norma. Ej: "armadores artesanales", "mayor escala".
        estado: Uno de 'vigente' | 'expirada' | 'derogada'. Ej: pasa 'vigente' para
            detectar antecedentes activos antes de actualizar/derogar.
        vigente_desde: Fecha YYYY-MM-DD. Acota a normas activas en o después de
            esta fecha (excluye normas ya derogadas/expiradas antes de esta fecha).
        vigente_hasta: Fecha YYYY-MM-DD. Acota a normas cuya vigencia_inicio sea
            en o antes de esta fecha. Combinado con vigente_desde delimita una
            ventana de tiempo específica (ej. "normas activas durante julio 2025").
        limite: Máximo de normas a devolver (1-50, por defecto 10).

    Returns:
        Para cada norma encontrada: sus campos (actores, objeto, accion, lugar,
        vigencia, estado, datos_dinamicos) y su línea de tiempo ordenada
        cronológicamente (fecha, tipo_cambio, nro_resolucion, descripción). Mensaje
        descriptivo si no hay resultados, si un filtro tiene un valor inválido, o
        si la consulta falla.
    """
    if accion is not None and accion not in ACCIONES_VALIDAS:
        return f"ERROR: accion debe ser uno de {sorted(ACCIONES_VALIDAS)}, recibido: '{accion}'."
    if estado is not None and estado not in ESTADOS_VALIDOS:
        return f"ERROR: estado debe ser uno de {sorted(ESTADOS_VALIDOS)}, recibido: '{estado}'."

    condiciones = []
    params: dict = {}
    if objeto:
        condiciones.append("objeto ILIKE :objeto")
        params["objeto"] = f"%{objeto}%"
    if accion:
        condiciones.append("accion = :accion")
        params["accion"] = accion
    if lugar:
        condiciones.append("lugar ILIKE :lugar")
        params["lugar"] = f"%{lugar}%"
    if actores:
        condiciones.append("actores ILIKE :actores")
        params["actores"] = f"%{actores}%"
    if estado:
        condiciones.append("estado = :estado")
        params["estado"] = estado
    if vigente_desde:
        # La norma sigue activa en o después de vigente_desde: o es indefinida
        # (vigencia_fin NULL) o su fin cae en/después de esa fecha.
        condiciones.append("(vigencia_fin IS NULL OR vigencia_fin >= :vigente_desde)")
        params["vigente_desde"] = vigente_desde
    if vigente_hasta:
        condiciones.append("vigencia_inicio <= :vigente_hasta")
        params["vigente_hasta"] = vigente_hasta

    where_sql = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
    params["limite"] = max(1, min(limite, 50))

    query_normas = text(
        f"""
        SELECT id, actores, objeto, accion, lugar, vigencia_inicio, vigencia_fin,
               estado, datos_dinamicos
        FROM normas_actuales
        {where_sql}
        ORDER BY vigencia_inicio DESC NULLS LAST, id DESC
        LIMIT :limite
        """
    )

    try:
        with engine.connect() as conn:
            normas = conn.execute(query_normas, params).mappings().all()
            if not normas:
                return "No se encontraron normas con los filtros especificados."

            bloques = [_formatear_norma_con_historial(conn, norma) for norma in normas]
            return "\n\n".join(bloques)
    except SQLAlchemyError as e:
        return f"Error al buscar normas: {e}"


@mcp.tool(
    title="Obtener norma por ID",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def obtener_norma(norma_id: int) -> str:
    """Ficha completa de UNA norma ya identificada por su norma_id, con su línea
    de tiempo completa (documentos que la crearon, actualizaron o derogaron).

    Cuándo usar:
    • Ya conoces el norma_id exacto (obtenido con buscar_normas o
      buscar_normas_por_resolucion, o devuelto por
      crear_norma/actualizar_norma/derogar_norma) y quieres su estado actual o
      reconfirmar el resultado de una escritura previa.

    Cuándo NO usar:
    • No conoces el norma_id → primero buscar_normas (filtros por
      objeto/accion/lugar/actores/estado/vigencia) o, si el texto referencia
      otra resolución por su número, buscar_normas_por_resolucion.

    Args:
        norma_id: ID numérico de la norma.

    Returns:
        Los campos de la norma (actores, objeto, accion, lugar, vigencia, estado,
        datos_dinamicos) y su línea de tiempo ordenada cronológicamente, o un
        mensaje si no existe una norma con ese ID.
    """
    try:
        with engine.connect() as conn:
            norma = conn.execute(
                text(
                    """
                    SELECT id, actores, objeto, accion, lugar, vigencia_inicio, vigencia_fin,
                           estado, datos_dinamicos
                    FROM normas_actuales
                    WHERE id = :norma_id
                    """
                ),
                {"norma_id": norma_id},
            ).mappings().first()

            if norma is None:
                return f"No existe ninguna norma con norma_id={norma_id}."

            return _formatear_norma_con_historial(conn, norma)
    except SQLAlchemyError as e:
        return f"Error al obtener la norma {norma_id}: {e}"


@mcp.tool(
    title="Buscar normas por número de resolución",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def buscar_normas_por_resolucion(nro_resolucion: str) -> str:
    """Busca las normas afectadas (creadas, actualizadas o derogadas) por el o los
    documentos cuyo número de resolución coincide, total o parcialmente.

    Cuándo usar:
    • El texto que estás procesando referencia OTRA resolución por su número
      (ej. "se deroga la norma establecida por R.M. 234-2025-PRODUCE", "modificada
      por R.D. 078-2025-PRODUCE") y quieres encontrar directamente la norma que
      ese documento afectó, sin adivinar su objeto o zona.

    Cuándo NO usar:
    • No tienes ningún número de resolución de referencia → usa buscar_normas
      con filtros por objeto/accion/lugar/actores/estado/vigencia.
    • Ya conoces el norma_id exacto → usa obtener_norma.

    La coincidencia es parcial e insensible a mayúsculas: tolera variaciones de
    formato (ej. nro_resolucion="234-2025" encuentra "R.M. 234-2025-PRODUCE").

    Args:
        nro_resolucion: Número de resolución, completo o parcial. Ej: "234-2025",
            "R.M. 234-2025-PRODUCE", "078-2025-PRODUCE".

    Returns:
        Para cada documento coincidente: su nro_resolucion, fecha de publicación
        y qué norma(s) creó/actualizó/derogó; seguido de la ficha completa (con
        línea de tiempo) de cada norma afectada. Mensaje descriptivo si no hay
        coincidencias o si la consulta falla.
    """
    try:
        with engine.connect() as conn:
            documentos = conn.execute(
                text(
                    """
                    SELECT id, nro_resolucion, fecha_publicacion, url_fuente
                    FROM documentos
                    WHERE nro_resolucion ILIKE :nro
                    ORDER BY fecha_publicacion DESC NULLS LAST, id DESC
                    """
                ),
                {"nro": f"%{nro_resolucion}%"},
            ).mappings().all()

            if not documentos:
                return f"No se encontró ningún documento cuyo número de resolución coincida con '{nro_resolucion}'."

            bloques = []
            norma_ids_afectados: list[int] = []
            for doc in documentos:
                cambios = conn.execute(
                    text(
                        """
                        SELECT norma_id, tipo_cambio
                        FROM linea_tiempo_normas
                        WHERE documento_id = :doc_id
                        ORDER BY id ASC
                        """
                    ),
                    {"doc_id": doc["id"]},
                ).mappings().all()

                doc_lines = [
                    f"Documento: {doc['nro_resolucion']}  (documento_id={doc['id']})",
                    f"  fecha_publicacion: {doc['fecha_publicacion'] or 'N/D'}  |  "
                    f"url_fuente: {doc['url_fuente'] or 'N/D'}",
                ]
                if cambios:
                    doc_lines.append(
                        "  Normas afectadas: "
                        + ", ".join(f"norma_id={c['norma_id']} ({c['tipo_cambio']})" for c in cambios)
                    )
                    norma_ids_afectados.extend(c["norma_id"] for c in cambios)
                else:
                    doc_lines.append("  No afectó ninguna norma registrada.")
                bloques.append("\n".join(doc_lines))

            norma_ids_unicos = sorted(set(norma_ids_afectados))
            if norma_ids_unicos:
                normas = conn.execute(
                    text(
                        """
                        SELECT id, actores, objeto, accion, lugar, vigencia_inicio, vigencia_fin,
                               estado, datos_dinamicos
                        FROM normas_actuales
                        WHERE id = ANY(:ids)
                        ORDER BY id
                        """
                    ),
                    {"ids": norma_ids_unicos},
                ).mappings().all()
                for norma in normas:
                    bloques.append(_formatear_norma_con_historial(conn, norma))

            return "\n\n".join(bloques)
    except SQLAlchemyError as e:
        return f"Error al buscar por número de resolución: {e}"



@mcp.tool(
    title="Crear norma nueva",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
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
    vigencia_fin: Optional[str] = None,
    url_fuente: Optional[str] = None,
    fecha_publicacion: Optional[str] = None,
    tipo: str = "resolucion",
) -> str:
    """Flujo CREACIÓN: registra una norma pesquera nueva que no tiene antecedente vigente en la BD.

    Cuándo usar:
    • Tras consultar con buscar_normas no se encontró ninguna norma vigente
      del mismo objeto/zona: la resolución establece algo enteramente nuevo.

    Cuándo NO usar:
    • Existe una norma vigente que solo cambia parámetros → actualizar_norma.
    • La resolución anula una norma vigente → derogar_norma.
    • confianza='baja' u objeto/accion no identificables → enviar_a_dlq (no llamar esta tool).

    Pasos internos (atómicos, en una sola transacción):
    1. Inserta el documento en `documentos` (idempotente por hash_pdf).
    2. Inserta la norma nueva en `normas_actuales` con estado='vigente'.
    3. Registra una entrada 'crea' en `linea_tiempo_normas`.

    Args:
        nro_resolucion: Número oficial de la resolución. Ej: "R.M. 234-2025-PRODUCE".
        hash_pdf: SHA-256 del PDF original (clave de idempotencia del documento).
        actores: Personas/entidades/embarcaciones sujetas a la norma, tal como
            aparecen en el texto. Ej: "armadores artesanales".
        objeto: Especie o recurso hidrobiológico. Ej: "anchoveta (Engraulis ringens)".
        accion: Uno de 'veda' | 'cuota' | 'permiso' | 'prohibicion' | 'otro'.
        lugar: Ámbito geográfico. Ej: "litoral norte-centro 04°S-16°S".
        vigencia_inicio: Fecha de entrada en vigor, formato YYYY-MM-DD.
        descripcion: Frase que resume la norma creada, para la línea de tiempo.
            Ej: "Creación de veda reproductiva de anchoveta en litoral norte-centro
            desde 2025-06-16 hasta 2025-07-31."
        datos_dinamicos_json: JSON con valores cuantitativos (cuota en TM, %,
            tallas, artes de pesca, etc.). Ej: '{"cuota_tm": 2500000}'. Default '{}'.
        confianza: 'alta' o 'media' (para 'baja' usar enviar_a_dlq en su lugar).
        vigencia_fin: Fecha de fin, formato YYYY-MM-DD, o None si es indefinida.
        url_fuente: URL del PDF original en el portal de PRODUCE, si se conoce.
        fecha_publicacion: Fecha de publicación oficial, formato YYYY-MM-DD.
        tipo: Tipo de documento. Default 'resolucion'.

    Returns:
        Confirmación con norma_id y documento_id asignados, o un mensaje de error
        si la escritura falla (ej. violación de constraint en `accion`).
    """
    try:
        datos = json.loads(datos_dinamicos_json) if datos_dinamicos_json else {}
    except json.JSONDecodeError:
        datos = {"raw": datos_dinamicos_json}

    try:
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
    except SQLAlchemyError as e:
        return f"Error al crear la norma: {e}"

    return f"Norma creada: norma_id={norma_id}, documento_id={doc_id}, confianza={confianza}."


@mcp.tool(
    title="Actualizar norma vigente",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def actualizar_norma(
    norma_id: int,
    nro_resolucion: str,
    hash_pdf: str,
    descripcion: str,
    actores: Optional[str] = None,
    objeto: Optional[str] = None,
    accion: Optional[str] = None,
    lugar: Optional[str] = None,
    vigencia_inicio: Optional[str] = None,
    vigencia_fin: Optional[str] = None,
    datos_dinamicos_json: Optional[str] = None,
    url_fuente: Optional[str] = None,
    fecha_publicacion: Optional[str] = None,
    tipo: str = "resolucion",
) -> str:
    """Flujo ACTUALIZACIÓN: modifica campos de una norma vigente ya existente en la BD.

    Cuándo usar:
    • La resolución cambia parámetros de una norma vigente sin derogarla: cuota,
      zona, fechas, actores. El antecedente se detecta con buscar_normas, o con
      buscar_normas_por_resolucion si el texto referencia la norma por el número
      de una resolución anterior.

    Cuándo NO usar:
    • No existe antecedente vigente → crear_norma.
    • La resolución anula la norma explícitamente → derogar_norma.
    • confianza='baja' u objeto/accion no identificables → enviar_a_dlq.

    Pasos internos (atómicos, en una sola transacción):
    1. Inserta el documento en `documentos` (idempotente por hash_pdf).
    2. Actualiza en `normas_actuales` solo los campos recibidos (no None).
    3. Registra una entrada 'actualiza' en `linea_tiempo_normas`.

    Args:
        norma_id: ID de la norma a actualizar, obtenido con buscar_normas o
            buscar_normas_por_resolucion.
        nro_resolucion: Número oficial de la resolución que origina el cambio.
        hash_pdf: SHA-256 del PDF original (clave de idempotencia del documento).
        descripcion: Frase que resume qué cambió, para la línea de tiempo.
            Ej: "Actualización de cuota de merluza de 50,000 TM a 40,000 TM
            por R.M. 301-2025-PRODUCE."
        actores: Nuevo valor, o None para dejarlo sin cambios.
        objeto: Nuevo valor, o None para dejarlo sin cambios.
        accion: Nuevo valor ('veda'|'cuota'|'permiso'|'prohibicion'|'otro'), o
            None para dejarlo sin cambios.
        lugar: Nuevo valor, o None para dejarlo sin cambios.
        vigencia_inicio: Nueva fecha YYYY-MM-DD, o None para dejarla sin cambios.
        vigencia_fin: Nueva fecha YYYY-MM-DD, o None para dejarla sin cambios.
        datos_dinamicos_json: JSON con los valores cuantitativos actualizados
            (reemplaza el objeto completo), o None para dejarlo sin cambios.
        url_fuente: URL del PDF original, si se conoce.
        fecha_publicacion: Fecha de publicación oficial, formato YYYY-MM-DD.
        tipo: Tipo de documento. Default 'resolucion'.

    Returns:
        Confirmación con la lista de campos modificados y el documento_id, o un
        mensaje de error si la escritura falla (ej. norma_id inexistente).
    """
    try:
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
    except SQLAlchemyError as e:
        return f"Error al actualizar la norma {norma_id}: {e}"

    campos_actualizados = [k for k in campos if k != "norma_id"]
    return f"Norma {norma_id} actualizada. Campos modificados: {campos_actualizados}. documento_id={doc_id}."


@mcp.tool(
    title="Derogar norma vigente",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def derogar_norma(
    norma_id: int,
    nro_resolucion: str,
    hash_pdf: str,
    descripcion: str,
    url_fuente: Optional[str] = None,
    fecha_publicacion: Optional[str] = None,
    tipo: str = "resolucion",
) -> str:
    """Flujo DEROGACIÓN: anula una norma vigente por disposición explícita de una resolución.

    Cuándo usar:
    • El texto dice explícitamente "se deroga", "se deja sin efecto", "se levanta
      la veda", etc., sobre una norma vigente detectada con buscar_normas o con
      buscar_normas_por_resolucion (más directo si el texto menciona el número
      de la resolución que estableció la norma original).

    Cuándo NO usar:
    • La resolución solo cambia parámetros sin anular la norma → actualizar_norma.
    • No hay antecedente vigente → crear_norma.
    • Si la misma resolución también crea una norma nueva, llama primero a esta
      tool y luego a crear_norma (mismo nro_resolucion y hash_pdf en ambas).

    Pasos internos (atómicos, en una sola transacción):
    1. Inserta el documento en `documentos` (idempotente por hash_pdf).
    2. Cambia el estado de la norma a 'derogada' en `normas_actuales`.
    3. Registra una entrada 'deroga' en `linea_tiempo_normas`.

    Args:
        norma_id: ID de la norma a derogar, obtenido con buscar_normas o
            buscar_normas_por_resolucion.
        nro_resolucion: Número oficial de la resolución que deroga la norma.
        hash_pdf: SHA-256 del PDF original (clave de idempotencia del documento).
        descripcion: Frase que explica la derogación y su origen, para la línea
            de tiempo. Ej: "Derogación de veda reproductiva de anchoveta por
            R.M. 289-2025-PRODUCE, que autoriza inicio de primera temporada 2025."
        url_fuente: URL del PDF original, si se conoce.
        fecha_publicacion: Fecha de publicación oficial, formato YYYY-MM-DD.
        tipo: Tipo de documento. Default 'resolucion'.

    Returns:
        Confirmación con el documento_id asociado, o un mensaje de error si la
        escritura falla (ej. norma_id inexistente).
    """
    try:
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
    except SQLAlchemyError as e:
        return f"Error al derogar la norma {norma_id}: {e}"

    return f"Norma {norma_id} marcada como derogada. documento_id={doc_id}."


@mcp.tool(
    title="Enviar a Dead Letter Queue",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def enviar_a_dlq(motivo: str, hash_pdf: Optional[str] = None, datos_parciales_json: str = "{}") -> str:
    """Desvía un documento a la Dead Letter Queue cuando la confianza de extracción es baja.

    Cuándo usar:
    • confianza='baja', u objeto/accion no son identificables con certeza en el
      texto del documento. Es la ruta obligatoria de la política "nunca inventar":
      nunca completar campos con valores aproximados o inferidos.

    Cuándo NO usar:
    • confianza='alta' o 'media' y objeto/accion sí son identificables → usa
      crear_norma, actualizar_norma o derogar_norma según corresponda.

    Efecto: registra el motivo y los datos parciales para revisión humana desde
    la interfaz HTML (tabla dlq_documentos, campo revisado=FALSE). No modifica
    `normas_actuales` ni `documentos`.

    Args:
        motivo: Explicación breve de por qué no se pudo procesar con confianza.
            Ej: "objeto no identificable: el texto habla de 'medidas de
            precaución' sin nombrar especie ni tipo de acción".
        hash_pdf: SHA-256 del PDF original, si se conoce (para trazabilidad).
        datos_parciales_json: JSON con los campos que sí se pudieron extraer,
            aunque incompletos. Default '{}'.

    Returns:
        Confirmación con el número de entrada asignado en la DLQ.
    """
    try:
        datos = json.loads(datos_parciales_json) if datos_parciales_json else {}
    except json.JSONDecodeError:
        datos = {"raw": datos_parciales_json}

    try:
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
    except SQLAlchemyError as e:
        return f"Error al enviar a DLQ: {e}"

    return f"Documento enviado a DLQ (entrada #{row[0]}). Motivo: {motivo}"


# ─── Resource de referencia ──────────────────────────────────────────────────


@mcp.resource("pesca://schema")
def resource_schema() -> str:
    """Referencia rápida de valores y forma de datos que las tools de escritura esperan.

    Consúltalo si no conoces los valores permitidos de `accion`/`estado` o el tipo
    de contenido esperado en `datos_dinamicos`, en vez de adivinarlos.

    URI: pesca://schema
    """
    return (
        "REFERENCIA DE DOMINIO — normas pesqueras de PRODUCE\n"
        + "=" * 48
        + "\n\n"
        + "accion (normas_actuales.accion) — valores permitidos:\n"
        + "  veda | cuota | permiso | prohibicion | otro\n\n"
        + "estado (normas_actuales.estado) — valores permitidos:\n"
        + "  vigente | expirada | derogada\n\n"
        + "tipo_cambio (linea_tiempo_normas.tipo_cambio) — valores permitidos:\n"
        + "  crea | actualiza | deroga | expira\n\n"
        + "datos_dinamicos (JSONB, libre) — incluir SIEMPRE los valores cuantitativos\n"
        + "presentes en el texto: cuota en TM, % de captura incidental, talla mínima,\n"
        + "artes de pesca permitidas, coordenadas exactas, etc. Ejemplo:\n"
        + '  {"cuota_tm": 30000, "talla_minima_cm": 35, "captura_incidental_pct": 10}\n\n'
        + "confianza (parámetro de crear_norma, no persistido) — valores permitidos:\n"
        + "  alta | media | baja  →  'baja' siempre debe ir a enviar_a_dlq, nunca a\n"
        + "  crear_norma/actualizar_norma/derogar_norma."
    )

if __name__ == "__main__":
    init_db()
    mcp.run(transport="http", host=config.MCP_SERVER_HOST, port=config.MCP_SERVER_PORT)
