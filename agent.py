"""Cliente MCP + agentes LangChain de la cascada de ingesta (Fichas 2 y 3).

Adaptación de `agent.py` (estructura del profesor: "Cliente MCP + agente LangChain.
Descubre tools y expone una función responder()."). Aquí la función expuesta es
`procesar_resolucion()`: descubre las tools del servidor FastMCP (`mcp_server.py`)
y encadena clasificador (Gemini Flash, sin tools) → agente extractor (Mistral Large
+ tools MCP), igual que la sección 2.2 del PoC.
"""
from __future__ import annotations

import base64

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
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

_PROMPT_TRANSCRIPCION_PDF = (
    "Ya se extrajo el texto plano de este documento (una resolución de PRODUCE) con un parseo "
    "estándar; te lo paso a continuación junto con el PDF original. Tu tarea es devolver el "
    "documento COMPLETO enriquecido en formato Markdown: usa el texto ya extraído como base y "
    "complétalo con la información que falta y que solo está visible en el PDF (tablas como "
    "tablas Markdown, mapas o zonas descritos con sus coordenadas/límites/nombres, anexos "
    "numéricos como cuotas, tallas, fechas o artes de pesca), insertando cada dato en el lugar "
    "exacto del documento donde aparece. No resumas ni interpretes el contenido normativo, no "
    "omitas nada del texto ya extraído, y no inventes datos que no estén en el PDF.\n\n"
    "--- TEXTO YA EXTRAÍDO (parseo estándar) ---\n{texto_extraido}"
)


async def _extraer_texto_multimodal(pdf_bytes: bytes, texto_extraido: str) -> str:
    """Fallback multimodal (Ficha 2): envía el PDF completo (no páginas renderizadas por 
    separado) junto con el texto ya extraído por `extraer_texto_pdf`, a Gemini Flash (soporte 
    nativo de archivos PDF), para que devuelva el documento COMPLETO enriquecido en un único 
    Markdown ordenado — combinando ese texto con las tablas, mapas y anexos numéricos que 
    solo están visibles en el PDF.
    """
    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    mensaje = HumanMessage(
        content=[
            {"type": "text", "text": _PROMPT_TRANSCRIPCION_PDF.format(texto_extraido=texto_extraido)},
            {"type": "file", "base64": pdf_base64, "mime_type": "application/pdf"},
        ]
    )
    respuesta = await gemini_flash.ainvoke([mensaje])
    return respuesta.content


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
    url_fuente: str = "",
    fecha_publicacion: str = "",
    tipo: str = "resolucion",
    pdf_bytes: bytes | None = None,
) -> dict:
    """Cascada completa (análogo a responder() del profesor):

    0. Deduplicación por hash_pdf.
    1. Clasificador (Gemini Flash) → relevancia y detección multimodal.
    1.b Si requiere_multimodal y se recibió `pdf_bytes`, envía el PDF completo + el texto ya
        extraído a Gemini Flash (soporte nativo de archivos PDF) para que devuelva el documento
        completo enriquecido en Markdown, y reclasifica con ese texto.
    2. Si relevante, agente extractor (Mistral + tools MCP) → extrae el nro_resolucion del
       propio texto, consulta BD, decide crear/actualizar/derogar, extrae y persiste autónomamente.

    `nro_resolucion` NO se recibe como parámetro: el agente extractor lo identifica del
    propio texto de la resolución (PASO 1 de `EXTRACCION_SYSTEM_PROMPT`), nunca como metadato
    dado por el usuario.

    `pdf_bytes` es opcional: sin él (ej. `/api/procesar/texto`, demos con texto ya
    extraído) el fallback multimodal no puede ejecutarse y el documento queda
    'pendiente_multimodal' para reprocesarse luego con el PDF original.
    """
    r = {"hash": hash_pdf[:16] + "..."}

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
        if pdf_bytes is None:
            r["estado"] = "pendiente_multimodal"
            r["motivo"] = (
                "PDF con mapas, tablas o anexos numéricos no capturados como texto — "
                "requiere reprocesar con el PDF original para el fallback multimodal"
            )
            return r

        texto = await _extraer_texto_multimodal(pdf_bytes, texto)

        clf2 = clasificador.invoke({"texto": texto})
        r["relevante"] = clf2.es_relevante
        if not clf2.es_relevante:
            r["estado"] = "descartado"
            r["motivo"] = clf2.motivo
            return r
        # El fallback multimodal ya se aplicó una vez: se continúa con el mejor texto
        # disponible aunque clf2.requiere_multimodal siga en True (evita reintentos en bucle).

    agente = await _get_agente_extractor()
    contexto_doc = (
        f"hash_pdf={hash_pdf}, url_fuente={url_fuente or 'N/A'}, "
        f"fecha_publicacion={fecha_publicacion or 'N/A'}, tipo={tipo} "
        "(nro_resolucion NO se entrega aquí: extráelo tú mismo del texto de la resolución)"
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
