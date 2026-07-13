"""Contrato de datos del MVP (Ficha 3), tal como se validó en el PoC (sección 1.2).

Estos dos schemas son el puente entre el texto libre de una resolución y las
tablas de PostgreSQL: `ClasificacionDocumento` enruta el documento, `NormaPesquera`
define exactamente lo que las tools de escritura del servidor MCP aceptan.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ClasificacionDocumento(BaseModel):
    """Resultado del clasificador (escudo de costos, Gemini Flash)."""

    es_relevante: bool = Field(
        description="True si el documento contiene normativa pesquera activa "
        "(vedas, cuotas, zonas de exclusión, artes permitidas, tallas mínimas, temporadas, permisos)."
    )
    requiere_multimodal: bool = Field(
        description="True si el PDF contiene mapas o tablas que el parseo estándar no puede "
        "extraer correctamente. Activa el fallback multimodal."
    )
    motivo: str = Field(description="Justificación breve de la clasificación (1-2 oraciones).")


class NormaPesquera(BaseModel):
    """Resultado del extractor (Mistral Large). Mínimos obligatorios: objeto, accion.

    Si no pueden extraerse con certeza, `confianza='baja'` para derivar a DLQ.
    """

    actores: str = Field(
        description="Personas, entidades o tipos de embarcación sujetos a la norma. "
        "Ej: 'embarcaciones de mayor escala', 'armadores artesanales'."
    )
    objeto: str = Field(
        description="Especie o recurso hidrobiológico sobre el que recae la norma. "
        "Ej: 'anchoveta (Engraulis ringens)', 'merluza'."
    )
    accion: Literal["veda", "cuota", "permiso", "prohibicion", "otro"] = Field(
        description="Tipo de acción normativa que establece la resolución."
    )
    lugar: str = Field(
        description="Ámbito geográfico donde aplica la norma: litoral, zona o coordenadas."
    )
    vigencia_inicio: str = Field(description="Fecha de entrada en vigor, formato YYYY-MM-DD.")
    vigencia_fin: Optional[str] = Field(
        default=None, description="Fecha de fin, formato YYYY-MM-DD. Null si es indefinida."
    )
    datos_dinamicos: dict = Field(
        default_factory=dict,
        description="Valores cuantitativos y condiciones específicas: cuota en TM, % de captura "
        "incidental, talla mínima, artes de pesca permitidas, fechas de temporada, etc.",
    )
    confianza: Literal["alta", "media", "baja"] = Field(
        description="Alta: campos críticos explícitos. Media: ambigüedades. "
        "Baja: faltan objeto o accion → el documento va a DLQ."
    )
