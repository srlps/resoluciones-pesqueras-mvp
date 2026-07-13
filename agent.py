"""Cliente MCP + agentes LangChain de la cascada de ingesta (Fichas 2 y 3).

Adaptación de `agent.py` (estructura del profesor: "Cliente MCP + agente LangChain.
Descubre tools y expone una función responder()."). Aquí la función expuesta es
`procesar_resolucion()`: descubre las tools del servidor FastMCP (`mcp_server.py`)
y encadena clasificador (Gemini Flash, sin tools) → agente extractor (Mistral Large
+ tools MCP), igual que la sección 2.2 del PoC.
"""
from __future__ import annotations

from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mistralai import ChatMistralAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from sqlalchemy import text

import config
from database import engine
from prompts import CLASIFICACION_PROMPT, EXTRACCION_SYSTEM_PROMPT
from schemas import ClasificacionDocumento

# ── Los dos modelos de la cascada (Ficha 2) ──────────────────────────────────
gemini_flash = ChatGoogleGenerativeAI(model=config.GEMINI_MODEL, temperature=0)
mistral_large = ChatMistralAI(model=config.MISTRAL_MODEL, temperature=0)

clasificador = CLASIFICACION_PROMPT | gemini_flash.with_structured_output(ClasificacionDocumento)

_mcp_client = MultiServerMCPClient(
    {
        "pesca_normas": {
            "url": config.MCP_SERVER_URL,
            "transport": "streamable_http",
        }
    }
)

_agente_extractor = None  # se construye de forma perezosa (necesita await para descubrir tools)


async def _get_agente_extractor():
    """Descubre las tools del servidor MCP y construye el agente extractor (una sola vez)."""
    global _agente_extractor
    if _agente_extractor is None:
        tools = await _mcp_client.get_tools()
        _agente_extractor = create_agent(
            model=mistral_large,
            tools=tools,
            system_prompt=EXTRACCION_SYSTEM_PROMPT,
        )
    return _agente_extractor


async def procesar_resolucion(
    texto: str,
    hash_pdf: str,
    nro_resolucion: str,
    url_fuente: str = "",
    fecha_publicacion: str = "",
    tipo: str = "resolucion",
) -> dict:
    """Cascada completa (análogo a responder() del profesor):

    0. Deduplicación por hash_pdf.
    1. Clasificador (Gemini Flash) → relevancia y detección multimodal.
    2. Si relevante, agente extractor (Mistral + tools MCP) → consulta BD, decide
       crear/actualizar/derogar, extrae y persiste autónomamente.
    """
    r = {"nro_resolucion": nro_resolucion, "hash": hash_pdf[:16] + "..."}

    with engine.connect() as conn:
        dup = conn.execute(text("SELECT id FROM documentos WHERE hash_pdf=:hash"), {"hash": hash_pdf}).fetchone()
        if dup:
            r["estado"] = "duplicado"
            r["motivo"] = "documento ya procesado (hash_pdf existente en BD)"
            return r

    clf = clasificador.invoke({"texto": texto})
    r["relevante"] = clf.es_relevante
    if not clf.es_relevante:
        r["estado"] = "descartado"
        r["motivo"] = clf.motivo
        return r

    if clf.requiere_multimodal:
        r["estado"] = "pendiente_multimodal"
        r["motivo"] = "PDF con mapas o tablas complejas — requiere fallback multimodal"
        return r

    agente = await _get_agente_extractor()
    contexto_doc = (
        f"nro_resolucion={nro_resolucion}, hash_pdf={hash_pdf}, "
        f"url_fuente={url_fuente or 'N/A'}, fecha_publicacion={fecha_publicacion or 'N/A'}, tipo={tipo}"
    )
    respuesta = await agente.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Extrae y persiste la(s) norma(s) pesquera(s) de esta resolución.\n\n"
                        f"Metadatos del documento: {contexto_doc}\n\n"
                        f"Texto de la resolución:\n{texto}"
                    ),
                }
            ]
        }
    )
    r["estado"] = "procesado_por_agente"
    r["respuesta_agente"] = respuesta["messages"][-1].content
    return r
