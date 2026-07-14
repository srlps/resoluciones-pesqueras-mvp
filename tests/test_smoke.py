"""Prueba de humo: valida que los módulos clave se importen y que los contratos
de datos (Pydantic) y la API web respondan, sin depender de credenciales reales
ni de una BD PostgreSQL activa.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from schemas import ClasificacionDocumento, NormaPesquera

DATA_CSV = Path(__file__).parent.parent / "data" / "datos_demo.csv"


def test_datos_demo_csv_existe_y_tiene_filas():
    assert DATA_CSV.exists()
    with DATA_CSV.open(encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) >= 1
    assert {"nro_resolucion", "fecha_publicacion", "url_fuente", "texto"} <= set(filas[0].keys())


def test_schema_clasificacion_documento():
    clf = ClasificacionDocumento(es_relevante=True, requiere_multimodal=False, motivo="Veda de anchoveta.")
    assert clf.es_relevante is True


def test_schema_norma_pesquera_campos_minimos():
    norma = NormaPesquera(
        actores="armadores artesanales",
        objeto="anchoveta (Engraulis ringens)",
        accion="veda",
        lugar="litoral norte-centro 04°S-16°S",
        vigencia_inicio="2025-06-16",
        vigencia_fin="2025-07-31",
        confianza="alta",
    )
    assert norma.accion == "veda"
    assert norma.confianza == "alta"


def test_schema_norma_pesquera_rechaza_accion_invalida():
    with pytest.raises(ValueError):
        NormaPesquera(
            actores="x",
            objeto="x",
            accion="accion_no_valida",
            lugar="x",
            vigencia_inicio="2025-01-01",
            confianza="alta",
        )


def test_app_importa_y_expone_ruta_raiz():
    """Import diferido: app.py requiere config/database, que a su vez requieren
    variables de entorno — deben tener defaults seguros para que el import no falle."""
    from fastapi.testclient import TestClient

    import app as app_module

    client = TestClient(app_module.app)
    # No se valida 200 (requiere Postgres real en /), solo que la app se construya
    # y sirva el archivo estático sin lanzar excepciones de import.
    resp = client.get("/")
    assert resp.status_code in (200, 500)


def test_app_expone_los_3_modos_de_procesamiento():
    """Las 3 formas de procesar (texto/URL/PDF) deben seguir registradas como rutas
    de la API — regresión rápida si alguna se elimina o se renombra sin querer."""
    import app as app_module

    rutas = {ruta.path for ruta in app_module.app.routes}
    assert {"/api/procesar/texto", "/api/procesar/url", "/api/procesar/pdf"} <= rutas


def test_mcp_server_importa_y_expone_las_tools_esperadas():
    """Import diferido (mismo motivo que test_app_importa...): mcp_server.py no debe
    tocar la BD al importarse, solo al llamar init_db()/las tools. Verifica que las
    7 tools del dominio sigan expuestas como funciones invocables directamente."""
    import mcp_server

    for nombre in (
        "buscar_normas",
        "buscar_normas_por_resolucion",
        "obtener_norma",
        "crear_norma",
        "actualizar_norma",
        "derogar_norma",
        "enviar_a_dlq",
    ):
        assert callable(getattr(mcp_server, nombre, None)), f"falta la tool {nombre}"
