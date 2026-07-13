"""Ingesta y parseo de PDFs (capa Raw → Silver), sección 1.1 del PoC.

Descarga el PDF de PRODUCE, calcula su hash SHA-256 (clave de idempotencia) y
extrae el texto con pdfplumber. Si el parseo estándar no basta (mapas, tablas
complejas), el llamador (agent.py) debe activar el fallback multimodal enviando
el PDF completo (bytes) a un modelo con soporte nativo de archivos PDF; ese
envío queda fuera de este módulo, que solo maneja el PDF, sin llamar a ningún LLM.
"""
from __future__ import annotations

import hashlib
import io

import httpx
import pdfplumber


def calcular_hash(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


def cargar_pdf_desde_url(url: str) -> tuple[bytes, str]:
    """Descarga un PDF del portal de PRODUCE. Devuelve (bytes_pdf, sha256_hex)."""
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    contenido = resp.content
    return contenido, calcular_hash(contenido)


def extraer_texto_pdf(pdf_bytes: bytes) -> str:
    """Extrae texto con pdfplumber (parseo estándar, sin mapas ni tablas complejas)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        paginas = [p.extract_text() or "" for p in pdf.pages]
    return "\n".join(paginas).strip()
