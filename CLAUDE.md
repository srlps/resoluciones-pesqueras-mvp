# CLAUDE.md

Referencia de contexto para asistentes de IA (Claude, Copilot, etc.) que trabajen en este
repositorio. Léelo antes de proponer cambios estructurales.

## Qué es este proyecto

MVP del "Asistente de Actualizaciones Pesqueras" (Perú): monitorea resoluciones de
**PRODUCE**, las clasifica y extrae con una cascada de dos modelos (Gemini Flash →
Mistral Large), y persiste las normas estructuradas en PostgreSQL con línea de tiempo
y Dead Letter Queue (DLQ) para casos de baja confianza.

Contexto completo (visión, arquitectura, contrato de datos): [docs/context.md](docs/context.md).
PoC de referencia (donde se validó toda la lógica antes de modularizarla): [docs/PoCNormasPesqueras.ipynb](docs/PoCNormasPesqueras.ipynb).

## Origen de la estructura

La estructura de archivos es una adaptación de la sugerida por el profesor del curso
(ver tabla de mapeo en [README.md](README.md)), cambiando Streamlit por una web HTML
estática servida por FastAPI, y agregando `schemas.py`, `pdf_service.py`, `database.py`
como módulos propios porque el pipeline en cascada los necesita testeables por separado.

**No agregar carpetas nuevas (`services/`, `app/`, `mcp/`, etc.) sin que el usuario lo pida
explícitamente.** El MVP es intencionalmente plano — es para aprendizaje/demo, no producción.

## Estado del desarrollo

- **2026-07-12** — Scaffold inicial completo: `app.py`, `agent.py`, `mcp_server.py`,
  `schemas.py`, `pdf_service.py`, `database.py`, `config.py`, `prompts.py`, `static/`,
  `data/datos_demo.csv`, `tests/test_smoke.py`, `README.md`, `.env.example`,
  `requirements.txt`. Todo el código es skeleton funcional derivado 1:1 del PoC del
  notebook, sin ejecutar aún contra una BD PostgreSQL real ni con claves API reales.
  Pendiente de revisión y aprobación del usuario (ver `REVIEW_CHECKLIST.md`).

> Actualiza esta sección con una entrada nueva cada vez que completes un cambio
> estructural o funcional relevante (nuevo archivo, cambio de contrato, nueva feature).
> No borres entradas anteriores — es un historial.

## Convenciones verificadas

- Español para nombres de dominio (normas, resoluciones, prompts); inglés/estándar para
  nombres técnicos genéricos (config, database, schemas).
- Cascada de ingesta: **Gemini Flash** clasifica (sin tools) → **Mistral Large** extrae
  y persiste (con tools MCP). No invertir el orden ni fusionar los dos pasos.
- Persistencia solo vía las tools de `mcp_server.py` (nunca SQL directo desde `agent.py`
  o `app.py`, salvo lecturas de solo-lectura para la interfaz en `app.py`).
- Política de incertidumbre "nunca inventar": campos críticos no identificables →
  `confianza='baja'` → `enviar_a_dlq`, nunca completar con valores aproximados.
- Sin RAG semántico para normas vigentes: siempre SQL determinista (ver `docs/context.md` §6).

## Antes de terminar cualquier tarea de cambio estructural

Según las reglas de flujo de trabajo del usuario (ver memoria de usuario), actualizar
en el mismo cambio:
- `README.md` (tabla de estructura / mapeo, sección de instrucciones si aplica)
- Este `CLAUDE.md` (nueva entrada en "Estado del desarrollo")
- `docs/context.md` §5 si la estructura de carpetas cambió
- `REVIEW_CHECKLIST.md` (agregar filas para archivos nuevos)
