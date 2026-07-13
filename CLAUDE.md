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
- **2026-07-12** — `mcp_server.py`: mejorada la discoverabilidad de las tools para
  clientes MCP genéricos (no solo el agente propio con `prompts.py`): `instructions`
  a nivel de servidor con mapa de tools y regla de encadenamiento, `title` +
  `annotations` (readOnlyHint/destructiveHint/idempotentHint/openWorldHint) por tool,
  docstrings reestructuradas con secciones `Cuándo usar` / `Cuándo NO usar` /
  `Args:` / `Returns:` (FastMCP las parsea para el schema JSON expuesto al modelo),
  manejo de errores uniforme en las tools de escritura (antes solo `ejecutar_consulta_sql`
  atrapaba excepciones), tipado `Optional[str] = None` en vez de `""` como sentinela
  de "sin cambios", un resource `pesca://schema` (enum de `accion`/`estado`, forma de
  `datos_dinamicos`) y un prompt `procesar_resolucion` que expone el flujo de decisión
  completo (crear/actualizar/derogar/DLQ) como plantilla reutilizable por cualquier
  cliente MCP.
- **2026-07-12** — `mcp_server.py`: reemplazada la tool de lectura `ejecutar_consulta_sql`
  (SQL libre) por `buscar_normas`, estructurada y parametrizada (`objeto`, `accion`,
  `lugar`, `estado`, `norma_id`, `limite`). Valida `accion`/`estado` contra los enums
  del dominio antes de consultar (ya no depende de que el modelo escriba SQL válido),
  y devuelve, por cada norma encontrada, su línea de tiempo completa (documentos que
  la crearon/actualizaron/derogaron) en vez de filas crudas. Actualizadas todas las
  referencias cruzadas en `prompts.py` y en los docstrings de `crear_norma`,
  `actualizar_norma`, `derogar_norma` y el prompt `procesar_resolucion`.
- **2026-07-12** — `mcp_server.py`: añadido filtro `actores` a `buscar_normas` (antes
  solo se seleccionaba en el SELECT pero no era filtrable ni se mostraba en el
  resultado formateado). Separada además la búsqueda por filtros de la búsqueda
  puntual: `buscar_normas` ya no acepta `norma_id` (quedó solo como punto de entrada
  por objeto/accion/lugar/actores/estado); se creó `obtener_norma(norma_id)` como
  tool independiente para la ficha + línea de tiempo de una norma ya identificada.
  Lógica de formateo compartida extraída a `_formatear_norma_con_historial()` para
  no duplicar código entre ambas tools.
- **2026-07-13** — `mcp_server.py`: añadidos filtros opcionales `vigente_desde` /
  `vigente_hasta` a `buscar_normas` para acotar resultados a una ventana de tiempo
  (overlap con `vigencia_inicio`/`vigencia_fin`, tolerando normas indefinidas). Nueva
  tool de lectura `buscar_normas_por_resolucion(nro_resolucion)`: cuando el texto de
  una resolución referencia OTRA resolución por su número (ej. "se deroga la norma de
  la R.M. 234-2025-PRODUCE"), busca el/los documentos con coincidencia parcial de
  `nro_resolucion` y devuelve qué norma(s) afectaron + la ficha completa de cada una,
  evitando que el agente tenga que adivinar objeto/zona para encontrar el antecedente.
  Reutiliza `_formatear_norma_con_historial()`. Actualizadas referencias en las
  `instructions` del servidor y en `prompts.py` (PASO 1).

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
