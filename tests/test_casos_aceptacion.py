"""Casos de aceptación exigidos por la rúbrica del curso (separados del smoke test,
que cubre solo los casos rápidos/mínimos/felices — ver `test_smoke.py`):

1. Caso feliz       — el agente usa la tool apropiada y entrega una respuesta útil
                       basada en datos disponibles.
2. Caso límite       — ante datos ausentes o incompletos, no inventa; indica la
                       falta de evidencia y orienta el siguiente paso (DLQ).
3. Fuera de alcance  — rechaza/descarta solicitudes (documentos) no cubiertas por
                       el caso de uso, sin llegar a invocar al agente extractor.
4. Tool inválida     — una entrada vacía, mal formada o no permitida devuelve un
                       error claro y no rompe la aplicación.
5. MCP no disponible — la UI comunica una falla de servicio sin exponer secretos
                       ni crear resultados ficticios.

Los casos 1, 2 y 3 ejecutan la cascada real (clasificador Gemini Flash + agente
extractor Mistral Large con tools MCP) porque validan comportamiento de agente que
no tiene sentido mockear — mockear "qué tool eligió el agente" haría la prueba
tautológica. Usan la fixture `mcp_de_prueba` (ver `conftest.py`): levanta el propio
`mcp_server.py` en un hilo de fondo contra una BD Postgres descartable, así que NO
hace falta tener `python mcp_server.py` corriendo a mano en otra terminal — solo un
servidor PostgreSQL alcanzable (mismo host/credenciales que `DATABASE_URL`).
Requieren además GOOGLE_API_KEY (y MISTRAL_API_KEY para los casos 1 y 2, que llegan
hasta el agente extractor) y se saltan automáticamente si algo falta. Los casos 4 y
5 son deterministas y corren siempre, sin red ni BD real.

Detalles de robustez de estos tests (no del pipeline en sí): todas las llamadas a
`procesar_resolucion` corren sobre el fixture `event_loop_compartido` (un único
event loop de asyncio para toda la sesión) en vez de `asyncio.run()` por test —
en Windows, reutilizar los clientes httpx async de `gemini_flash`/`mistral_large`
(singletons de módulo en `agent.py`) entre loops distintos revienta con
`RuntimeError: Event loop is closed`. Además, `_procesar_con_reintento()` reintenta
ante un 429 de Mistral/Gemini con espera creciente — pero si la cuota agotada es la
DIARIA de un tier gratuito (ej. Gemini free tier: 20 solicitudes/día), estos tests
seguirán fallando hasta el día siguiente sin importar el reintento.

Casos base tomados de la sección 3.2 del PoC (`docs/PoCNormasPesqueras.ipynb`),
adaptados a las diferencias del MVP frente al PoC:
  - `procesar_resolucion()` ya NO recibe `nro_resolucion` como parámetro: el agente
    lo extrae del propio texto (antes era un argumento del caller).
  - La tool de lectura ya no es SQL libre (`ejecutar_consulta_sql`) sino
    `buscar_normas` / `buscar_normas_por_resolucion`, estructuradas y validadas.
  - `enviar_a_dlq` persiste en la tabla `dlq_documentos` (en el PoC era una lista
    en memoria), lo que permite verificar el caso límite consultando la BD en vez
    de una variable global del notebook.
La derogación por número de resolución (caso feliz #2, abajo) es un caso
enteramente nuevo del MVP: ejercita `buscar_normas_por_resolucion`, tool que no
existía en el PoC.
"""
from __future__ import annotations

import hashlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app as app_module
import config
import mcp_server
from agent import procesar_resolucion

_TIENE_CLAVES_LLM = bool(config.GOOGLE_API_KEY) and bool(config.MISTRAL_API_KEY)

_RAZON_FALTA_CLAVES_LLM = "Requiere GOOGLE_API_KEY + MISTRAL_API_KEY (cascada completa)."
_RAZON_FALTA_CLAVE_CLASIFICADOR = "Requiere GOOGLE_API_KEY (clasificador)."


def _hash_unico(texto: str, sufijo: str) -> str:
    """Hash determinista pero único por caso de prueba, para no chocar con el
    hash_pdf de otra corrida ni con datos reales ya persistidos."""
    return hashlib.sha256(f"{texto}::{sufijo}".encode()).hexdigest()


def _es_error_de_rate_limit(exc: Exception) -> bool:
    """Detecta un 429 sin acoplarse a la clase de excepción específica de cada SDK:
    Mistral lo propaga como `httpx.HTTPStatusError`; Gemini lo envuelve en
    `ChatGoogleGenerativeAIError`/`google.genai.errors.APIError` con "429"/
    "RESOURCE_EXHAUSTED" en el mensaje. Coincidencia por texto para no importar
    tipos internos de cada proveedor solo para esto."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    texto_error = str(exc)
    return "429" in texto_error or "RESOURCE_EXHAUSTED" in texto_error or "rate_limited" in texto_error.lower()


def _procesar_con_reintento(event_loop_compartido, **kwargs):
    """Ejecuta `procesar_resolucion` en el loop compartido, reintentando ante un 429
    (rate limit) de Mistral o Gemini. Los tests de cascada completa disparan varias
    llamadas reales seguidas en poco tiempo (clasificar + agente extractor, a veces
    dos resoluciones por test) y pueden toparse con el límite de la API key usada
    para correrlos — no es un fallo del pipeline, es una limitación de cuota.

    Nota: si la cuota agotada es la DIARIA de un tier gratuito (ej. Gemini free tier:
    20 solicitudes/día), ningún reintento con espera corta la va a levantar hasta el
    día siguiente — en ese caso los tests seguirán fallando de forma consistente y
    hay que esperar al reset de cuota o usar una API key de un tier con más margen.
    """
    intentos_restantes = 3
    espera_seg = 10
    while True:
        try:
            return event_loop_compartido.run_until_complete(procesar_resolucion(**kwargs))
        except Exception as e:  # noqa: BLE001
            intentos_restantes -= 1
            if not _es_error_de_rate_limit(e) or intentos_restantes <= 0:
                raise
            time.sleep(espera_seg)
            espera_seg *= 2


# ─────────────────────────────────────────────────────────────────────────────
# 1. Caso feliz
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _TIENE_CLAVES_LLM, reason=_RAZON_FALTA_CLAVES_LLM)
def test_caso_feliz_crea_norma_con_datos_completos(mcp_de_prueba, event_loop_compartido):
    """Resolución con todos los campos explícitos (especie, cuota, zona, vigencia,
    talla mínima): el agente debe clasificarla como relevante, usar `buscar_normas`
    (no encuentra antecedente) + `crear_norma`, y persistir una norma vinculada al
    documento — una respuesta útil y verificable, no solo texto plausible."""
    texto = (
        "RESOLUCIÓN MINISTERIAL N° 067-2025-PRODUCE. Lima, 12 febrero 2025. "
        "Se establece la cuota global de captura de merluza (Merluccius gayi peruanus) "
        "para la temporada 2025, correspondiente a 30,000 toneladas métricas. "
        "Zona de pesca: litoral peruano entre los 03°30'S y 16°00'S. "
        "Vigencia: desde el 15 de febrero hasta el 15 de agosto de 2025. "
        "Talla mínima de captura: 35 cm de longitud total."
    )
    h = _hash_unico(texto, "caso_feliz_crea")

    res = _procesar_con_reintento(
        event_loop_compartido, texto=texto, hash_pdf=h, fecha_publicacion="2025-02-12"
    )

    assert res["relevante"] is True
    assert res["estado"] == "procesado_por_agente"
    assert res["respuesta_agente"]

    with mcp_de_prueba.connect() as conn:
        norma = conn.execute(
            text(
                """
                SELECT n.id, n.objeto, n.accion FROM normas_actuales n
                JOIN linea_tiempo_normas lt ON lt.norma_id = n.id AND lt.tipo_cambio = 'crea'
                JOIN documentos d ON lt.documento_id = d.id
                WHERE d.hash_pdf = :hash
                """
            ),
            {"hash": h},
        ).mappings().first()

    assert norma is not None, "El agente respondió pero no persistió ninguna norma vinculada al documento"
    assert "merluza" in norma["objeto"].lower()
    assert norma["accion"] == "cuota"


@pytest.mark.skipif(not _TIENE_CLAVES_LLM, reason=_RAZON_FALTA_CLAVES_LLM)
def test_caso_feliz_deroga_norma_referenciada_por_numero_de_resolucion(mcp_de_prueba, event_loop_compartido):
    """Caso exclusivo del MVP (no existía en el PoC): una segunda resolución deroga
    la primera citándola por su número ("R.M. 067-2025-PRODUCE"). El agente debe
    usar `buscar_normas_por_resolucion` (no `buscar_normas` por objeto/zona) para
    ubicar la norma exacta y llamar `derogar_norma` — verificado consultando el
    estado real en la BD, no solo el texto de la respuesta."""
    texto_original = (
        "RESOLUCIÓN MINISTERIAL N° 067-2025-PRODUCE. Lima, 12 febrero 2025. "
        "Se establece la cuota global de captura de merluza (Merluccius gayi peruanus) "
        "para la temporada 2025, correspondiente a 30,000 toneladas métricas. "
        "Zona de pesca: litoral peruano entre los 03°30'S y 16°00'S. "
        "Vigencia: desde el 15 de febrero hasta el 15 de agosto de 2025."
    )
    h_original = _hash_unico(texto_original, "caso_feliz_deroga_original")
    _procesar_con_reintento(
        event_loop_compartido, texto=texto_original, hash_pdf=h_original, fecha_publicacion="2025-02-12"
    )

    texto_derogatorio = (
        "RESOLUCIÓN MINISTERIAL N° 099-2025-PRODUCE. Lima, 20 agosto 2025. "
        "Se deroga la cuota de captura de merluza establecida por la "
        "R.M. 067-2025-PRODUCE, al haberse alcanzado el límite autorizado."
    )
    h_derogatorio = _hash_unico(texto_derogatorio, "caso_feliz_deroga_derogatorio")

    res = _procesar_con_reintento(
        event_loop_compartido, texto=texto_derogatorio, hash_pdf=h_derogatorio, fecha_publicacion="2025-08-20"
    )

    assert res["relevante"] is True
    assert res["estado"] == "procesado_por_agente"

    with mcp_de_prueba.connect() as conn:
        norma = conn.execute(
            text(
                """
                SELECT n.estado FROM normas_actuales n
                JOIN linea_tiempo_normas lt ON lt.norma_id = n.id AND lt.tipo_cambio = 'crea'
                JOIN documentos d ON lt.documento_id = d.id
                WHERE d.hash_pdf = :hash
                """
            ),
            {"hash": h_original},
        ).mappings().first()

    assert norma is not None
    assert norma["estado"] == "derogada", "La norma original debió quedar derogada tras la 2da resolución"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Caso límite — datos ausentes/incompletos: no inventar, ir a DLQ
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _TIENE_CLAVES_LLM, reason=_RAZON_FALTA_CLAVES_LLM)
def test_caso_limite_accion_indeterminada_no_inventa_y_va_a_dlq(mcp_de_prueba, event_loop_compartido):
    """Texto relevante para pesca (evaluación IMARPE sobre flota de arrastre) pero
    sin especie ni tipo de acción normativa concretos todavía ('las medidas
    específicas serán determinadas...'). El agente NO debe inventar objeto/accion
    ni crear una norma: debe reconocer la falta de evidencia (confianza='baja') y
    enviarlo a la DLQ para revisión humana — política 'nunca inventar' de la Ficha 1."""
    texto = (
        "RESOLUCIÓN DIRECTORAL N° 078-2025-PRODUCE. Lima, 3 de octubre de 2025. "
        "En atención a los resultados del crucero de evaluación de recursos demersales "
        "efectuado por el IMARPE durante setiembre de 2025, se dispone la adopción de "
        "medidas de precaución para los recursos objetivo de la flota de arrastre de fondo "
        "del litoral norte. Las medidas específicas serán determinadas en coordinación "
        "con los gremios de armadores en el plazo de setenta y dos (72) horas."
    )
    h = _hash_unico(texto, "caso_limite_dlq")

    with mcp_de_prueba.connect() as conn:
        dlq_antes = conn.execute(text("SELECT COUNT(*) FROM dlq_documentos")).scalar()

    res = _procesar_con_reintento(
        event_loop_compartido, texto=texto, hash_pdf=h, fecha_publicacion="2025-10-03"
    )

    assert res["relevante"] is True  # el clasificador sí lo considera normativa pesquera potencial
    assert res["estado"] == "procesado_por_agente"

    with mcp_de_prueba.connect() as conn:
        dlq_despues = conn.execute(text("SELECT COUNT(*) FROM dlq_documentos")).scalar()
        norma_inventada = conn.execute(
            text(
                """
                SELECT n.id FROM normas_actuales n
                JOIN linea_tiempo_normas lt ON lt.norma_id = n.id
                JOIN documentos d ON lt.documento_id = d.id
                WHERE d.hash_pdf = :hash
                """
            ),
            {"hash": h},
        ).fetchone()

    assert dlq_despues > dlq_antes, "El documento con acción indeterminada debió derivarse a la DLQ"
    assert norma_inventada is None, "El agente no debió crear una norma sin objeto/accion identificables"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fuera de alcance — descarta documentos sin efecto regulatorio pesquero
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not config.GOOGLE_API_KEY, reason=_RAZON_FALTA_CLAVE_CLASIFICADOR)
def test_fuera_de_alcance_descarta_documento_no_pesquero(mcp_de_prueba, event_loop_compartido):
    """Una resolución válida de PRODUCE pero sin efecto regulatorio pesquero (aprueba
    un reglamento de minería artesanal) debe ser descartada por el clasificador
    ANTES de invocar al agente extractor — nunca se le pide a Mistral que extraiga
    una norma pesquera de un texto que no la contiene."""
    texto = (
        "RESOLUCIÓN MINISTERIAL N° 178-2025-PRODUCE. Lima, 10 mayo 2025. "
        "Se aprueba el Reglamento de Formalización de la Minería Artesanal y de Pequeña Escala. "
        "Se establecen los requisitos para la obtención del Instrumento de Gestión Ambiental "
        "Correctivo (IGAC) para actividades de extracción de minerales en el territorio nacional. "
        "Las operaciones mineras deben cumplir con los límites máximos permisibles establecidos."
    )
    h = _hash_unico(texto, "fuera_de_alcance_mineria")

    res = _procesar_con_reintento(
        event_loop_compartido, texto=texto, hash_pdf=h, fecha_publicacion="2025-05-10"
    )

    assert res["relevante"] is False
    assert res["estado"] == "descartado"
    assert res["motivo"], "El descarte debe venir acompañado de una justificación, no silencioso"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tool inválida — entrada vacía/mal formada/no permitida no rompe la app
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_invalida_buscar_normas_rechaza_accion_no_permitida():
    """`buscar_normas` valida `accion` contra el enum del dominio ANTES de tocar la
    BD: una acción no permitida devuelve un mensaje de error claro, no una excepción
    ni una consulta SQL con un valor arbitrario."""
    resultado = mcp_server.buscar_normas(accion="veda_ilegal")
    assert resultado.startswith("ERROR")
    assert "veda_ilegal" in resultado


def test_tool_invalida_buscar_normas_rechaza_estado_no_permitido():
    resultado = mcp_server.buscar_normas(estado="en_tramite")
    assert resultado.startswith("ERROR")
    assert "en_tramite" in resultado


def test_tool_invalida_endpoint_texto_vacio_o_malformado_no_rompe_la_app():
    """Un body vacío, sin el campo requerido, o JSON malformado en /api/procesar/*
    debe devolver un error de validación claro (4xx) en vez de una excepción no
    controlada — la app sigue sirviendo otras rutas después de la request inválida."""
    # Sin `with`: no dispara el lifespan (init_db), igual que en test_smoke.py —
    # estas rutas fallan en la validación de Pydantic antes de tocar la BD.
    client = TestClient(app_module.app)

    resp_vacio = client.post("/api/procesar/texto", json={})
    assert resp_vacio.status_code == 422

    resp_malformado = client.post(
        "/api/procesar/texto",
        content=b"esto no es json",
        headers={"content-type": "application/json"},
    )
    assert resp_malformado.status_code in (400, 422)

    resp_pdf_sin_archivo = client.post("/api/procesar/pdf")
    assert resp_pdf_sin_archivo.status_code == 422

    # La app sigue viva después de las requests inválidas.
    resp_normal = client.get("/")
    assert resp_normal.status_code in (200, 500)


def test_tool_invalida_url_no_descargable_devuelve_400_no_500():
    """/api/procesar/url con una URL que no se puede descargar (host inalcanzable)
    debe devolver 400 con un motivo claro, no un 500 sin manejar."""
    client = TestClient(app_module.app)
    resp = client.post(
        "/api/procesar/url",
        json={"url": "http://localhost:1/no-existe.pdf", "fecha_publicacion": ""},
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# 5. MCP no disponible — falla de servicio sin exponer secretos ni inventar datos
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_no_disponible_responde_503_sin_exponer_secretos(monkeypatch):
    """Si `procesar_resolucion` falla (MCP caído, BD inalcanzable, error del modelo),
    la UI debe recibir un error de servicio genérico (503) — nunca un resultado
    inventado y nunca el detalle interno de la excepción, que en este ejemplo
    simula contener una cadena de conexión con credenciales."""

    async def _falla_como_si_mcp_estuviera_caido(**kwargs):
        raise ConnectionError(
            "Connection refused: postgresql+psycopg2://postgres:SUPER_SECRETO@localhost:5432/pesca_mvp"
        )

    monkeypatch.setattr(app_module, "procesar_resolucion", _falla_como_si_mcp_estuviera_caido)

    client = TestClient(app_module.app)
    resp = client.post("/api/procesar/texto", json={"texto": "cualquier texto de prueba"})

    assert resp.status_code == 503
    cuerpo = resp.text
    assert "SUPER_SECRETO" not in cuerpo
    assert "postgres:SUPER_SECRETO" not in cuerpo
    detalle = resp.json()["detail"].lower()
    assert "no disponible" in detalle
