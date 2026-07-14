"""Interfaz web: FastAPI sirviendo HTML estático + API para procesar resoluciones,
ver la línea de tiempo de normas y revisar la DLQ.

Adaptación de `app.py` (estructura del profesor: "entrada del usuario, respuesta,
mensajes de estado y visualización") reemplazando Streamlit por HTML/CSS/JS estático
servido directamente por FastAPI.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

import config
from agent import procesar_resolucion
from database import engine, init_db
from pdf_service import cargar_pdf_desde_url, calcular_hash, extraer_texto_pdf

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Asistente de Actualizaciones Pesqueras — MVP", lifespan=_lifespan)


class ProcesarTextoRequest(BaseModel):
    texto: str
    fecha_publicacion: str = ""
    url_fuente: str = ""


class ProcesarUrlRequest(BaseModel):
    url: str
    fecha_publicacion: str = ""


_MENSAJE_SERVICIO_NO_DISPONIBLE = (
    "Servicio de procesamiento no disponible en este momento. Intenta nuevamente más tarde."
)


async def _procesar_o_503(**kwargs) -> dict:
    """Envuelve procesar_resolucion: si el servidor MCP, la BD o algún modelo de la cascada
    fallan (ej. MCP caído), la UI recibe un error de servicio genérico en vez de una
    excepción cruda sin manejar — que podría exponer detalles internos (cadenas de
    conexión, hosts, etc.) o tumbar la request con un 500 sin explicación.
    """
    try:
        return await procesar_resolucion(**kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"Error interno en procesar_resolucion: {e}")
        raise HTTPException(status_code=503, detail=_MENSAJE_SERVICIO_NO_DISPONIBLE) from e


@app.post("/api/procesar/texto")
async def procesar_texto(req: ProcesarTextoRequest) -> dict:
    """Procesa una resolución a partir de texto ya extraído (uso principal: demos y pruebas).

    El nro_resolucion NO se pide aquí: el agente extractor lo identifica del propio texto.
    """
    h = calcular_hash(req.texto.encode())
    return await _procesar_o_503(
        texto=req.texto,
        hash_pdf=h,
        url_fuente=req.url_fuente,
        fecha_publicacion=req.fecha_publicacion,
    )


@app.post("/api/procesar/url")
async def procesar_url(req: ProcesarUrlRequest) -> dict:
    """Descarga el PDF desde una URL de PRODUCE, extrae texto y ejecuta la cascada.

    El nro_resolucion NO se pide aquí: el agente extractor lo identifica del propio texto.
    """
    try:
        contenido, h = cargar_pdf_desde_url(req.url)
        texto = extraer_texto_pdf(contenido)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"No se pudo procesar el PDF: {e}") from e

    return await _procesar_o_503(
        texto=texto,
        hash_pdf=h,
        url_fuente=req.url,
        fecha_publicacion=req.fecha_publicacion,
        pdf_bytes=contenido,
    )


@app.post("/api/procesar/pdf")
async def procesar_pdf(archivo: UploadFile = File(...), fecha_publicacion: str = "") -> dict:
    """Procesa una resolución a partir de un PDF subido directamente desde la interfaz.

    El nro_resolucion NO se pide aquí: el agente extractor lo identifica del propio texto.
    """
    try:
        contenido = await archivo.read()
        h = calcular_hash(contenido)
        texto = extraer_texto_pdf(contenido)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"No se pudo procesar el PDF: {e}") from e

    return await _procesar_o_503(
        texto=texto,
        hash_pdf=h,
        url_fuente=archivo.filename or "",
        fecha_publicacion=fecha_publicacion,
        pdf_bytes=contenido,
    )


@app.get("/api/normas")
def listar_normas(estado: str | None = None) -> list[dict]:
    """Línea de tiempo de normas para la interfaz HTML."""
    query = """
        SELECT n.id, n.actores, n.objeto, n.accion, n.lugar, n.vigencia_inicio,
               n.vigencia_fin, n.estado, n.datos_dinamicos,
               lt.tipo_cambio, lt.fecha_cambio, lt.descripcion, d.nro_resolucion
        FROM normas_actuales n
        JOIN linea_tiempo_normas lt ON lt.norma_id = n.id
        JOIN documentos d ON lt.documento_id = d.id
        {where}
        ORDER BY lt.fecha_cambio DESC
    """.format(where="WHERE n.estado = :estado" if estado else "")
    with engine.connect() as conn:
        rows = conn.execute(text(query), {"estado": estado} if estado else {}).mappings().all()
    return [dict(r) for r in rows]


@app.get("/api/dlq")
def listar_dlq(solo_pendientes: bool = True) -> list[dict]:
    """Documentos de baja confianza pendientes de revisión humana."""
    query = "SELECT * FROM dlq_documentos"
    if solo_pendientes:
        query += " WHERE revisado = FALSE"
    query += " ORDER BY fecha_creacion DESC"
    with engine.connect() as conn:
        rows = conn.execute(text(query)).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/dlq/{dlq_id}/revisar")
def marcar_dlq_revisado(dlq_id: int) -> dict:
    """Marca una entrada de la DLQ como revisada por un humano."""
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE dlq_documentos SET revisado = TRUE WHERE id = :id"), {"id": dlq_id}
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Entrada de DLQ no encontrada")
    return {"id": dlq_id, "revisado": True}


# ── HTML estático (sirve static/index.html en la raíz) ───────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.APP_HOST, port=config.APP_PORT, reload=True)
