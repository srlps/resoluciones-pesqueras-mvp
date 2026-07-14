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
- **2026-07-13** — `prompts.py`: corregido `CLASIFICACION_PROMPT` para no mezclar
  relevancia y multimodalidad. Antes, cualquier dato multimodal faltante forzaba
  `es_relevante=True`; ahora `es_relevante` se decide primero por el propósito
  regulatorio completo del documento (puede ser `False` aunque falten tablas/mapas
  si es evidente que el documento es ajeno a la pesca). `requiere_multimodal=True`
  solo aplica junto con `es_relevante=True`, en dos escenarios: (1) relevancia aún
  no concluida por falta de datos multimodales, o (2) relevancia ya confirmada pero
  faltan datos multimodales que el extractor necesita (cuotas, tallas, coordenadas).
- **2026-07-13** — Implementado el fallback multimodal end-to-end (antes solo
  marcaba `pendiente_multimodal` sin actuar). `agent.py`: nueva
  `_extraer_texto_multimodal(pdf_bytes, texto_extraido)` envía el **PDF completo**
  (no páginas renderizadas por separado) junto con el texto ya extraído por
  `extraer_texto_pdf`, a Gemini Flash usando su soporte nativo de archivos PDF
  (content block `{"type": "file", "base64": ..., "mime_type": "application/pdf"}`),
  pidiendo que devuelva el documento **completo enriquecido en un único Markdown
  ordenado** (usa el texto ya extraído como base y lo completa con tablas/mapas/
  anexos numéricos que solo están visibles en el PDF, insertados en el lugar exacto
  donde aparecen) en vez de una transcripción separada a concatenar;
  `procesar_resolucion()` ahora acepta `pdf_bytes: bytes | None = None` — si
  `clf.requiere_multimodal` y hay `pdf_bytes`, reemplaza `texto` por el Markdown
  enriquecido y reclasifica una sola vez (sin loop) antes de continuar al agente
  extractor; sin `pdf_bytes` (ej. endpoint de solo texto) se mantiene el estado
  `pendiente_multimodal` como antes. `app.py`: el endpoint `/api/procesar/url`
  ahora pasa `pdf_bytes=contenido` para habilitar el fallback (el endpoint
  `/api/procesar/texto`, sin PDF, no puede activarlo). `pdf_service.py` y
  `requirements.txt` quedan sin cambios (no se necesita rasterizar páginas ni
  PyMuPDF; Gemini procesa el PDF directamente).
- **2026-07-13** — La interfaz web ahora tiene 3 formas de procesar (antes solo texto), y
  `nro_resolucion` ya no lo ingresa el usuario: el agente extractor lo identifica del propio
  texto de la resolución (nunca como metadato). `prompts.py`: `EXTRACCION_SYSTEM_PROMPT` tiene
  un nuevo PASO 1 explícito para extraer nro_resolucion del texto (si no aparece, usa "N/D" —
  eso NO baja la confianza por sí solo, solo indica que el documento es un comunicado u otro
  tipo distinto a una resolución; la confianza depende únicamente de qué tan ciertos son los
  campos normativos); los pasos siguientes se renumeraron. `agent.py`:
  `procesar_resolucion()` ya no recibe `nro_resolucion` como parámetro; `contexto_doc` aclara
  al agente que debe extraerlo él mismo. `app.py`: `ProcesarTextoRequest`/`ProcesarUrlRequest`
  perdieron el campo `nro_resolucion`; nuevo endpoint `POST /api/procesar/pdf` (`UploadFile`,
  requiere `python-multipart` — agregado a `requirements.txt`) para subir un PDF directamente
  sin pasar por una URL. `static/index.html`/`app.js`/`style.css`: reemplazado el formulario
  único por 3 pestañas (Texto / URL / Subir PDF) sin campo de N° de resolución, cada una
  llamando a su endpoint correspondiente (JSON para texto/url, `FormData` para pdf).
- **2026-07-13** — `static/style.css`: corregido bug de renderizado en las 3 pestañas
  (Texto/URL/Subir PDF) — las 3 aparecían apiladas simultáneamente en vez de alternar.
  Causa: la regla `form { display: flex; ... }` (CSS de autor) pisa el comportamiento
  por defecto de `[hidden]` del navegador (CSS de user-agent), sin importar
  especificidad. Se agregó `form[hidden] { display: none; }` justo después de la
  regla base de `form` para restaurar el comportamiento esperado.
- **2026-07-13** — Nuevos tests de aceptación, separados del smoke test:
  `tests/test_casos_aceptacion.py` cubre los 5 casos mínimos exigidos por la rúbrica
  del curso (caso feliz, caso límite, fuera de alcance, tool inválida, MCP no
  disponible), reutilizando y adaptando los casos del PoC (sección 3.2 del notebook)
  a las diferencias del MVP (sin `nro_resolucion` como parámetro de
  `procesar_resolucion`; `buscar_normas`/`buscar_normas_por_resolucion` en vez de SQL
  libre; DLQ persistida en `dlq_documentos` en vez de una lista en memoria). Incluye
  un caso exclusivo del MVP (derogación de una norma referenciada por número de
  resolución, vía `buscar_normas_por_resolucion`, tool que no existía en el PoC). Los
  casos feliz/límite ejecutan la cascada real (requieren credenciales + Postgres +
  `mcp_server.py` corriendo) y se saltan automáticamente si esa infraestructura no
  está disponible; fuera de alcance/tool inválida/MCP no disponible corren siempre
  (los dos últimos no dependen de red ni BD real). `app.py`: para soportar el caso
  "MCP no disponible" de forma real (antes un fallo del agente extractor se
  propagaba como una excepción sin manejar), se agregó el helper `_procesar_o_503`
  que envuelve las 3 llamadas a `procesar_resolucion` y convierte cualquier falla
  interna (MCP caído, BD inalcanzable, error del modelo) en un `503` con un mensaje
  genérico, sin exponer el detalle de la excepción original (que podría contener
  cadenas de conexión). `tests/test_smoke.py`: agregadas 2 verificaciones
  estructurales rápidas (las 3 rutas de procesamiento siguen registradas en `app.py`;
  `mcp_server.py` sigue exponiendo las 7 tools esperadas como funciones invocables),
  sin agregar nada que dependa de red o BD real.
- **2026-07-13** — Nuevo `tests/conftest.py`: fixture `mcp_de_prueba` que levanta el
  propio `mcp_server.py` en un hilo de fondo (puerto 8766, distinto del real 8765)
  contra una base PostgreSQL descartable (`<db>_test`, recreada con DROP+CREATE al
  inicio de cada sesión de pytest) — **ya no hace falta tener `python mcp_server.py`
  corriendo a mano** para los casos feliz/límite/fuera de alcance de
  `test_casos_aceptacion.py` (supera lo dicho en la entrada anterior); solo se
  necesita un servidor PostgreSQL alcanzable (mismo host/credenciales que
  `DATABASE_URL`) + las API keys. No es una BD "en memoria" real (Postgres no
  soporta eso y el esquema usa JSONB/`CAST(...AS jsonb)`/`ON CONFLICT`, sintaxis que
  SQLite no entiende sin reescribir el SQL de producción) — es el equivalente
  práctico: descartable y aislada de `DATABASE_URL`. La fixture redirige
  `mcp_server.engine` y `agent.engine` a la BD de prueba y reapunta
  `agent._mcp_client` al puerto de test (reseteando `agent._agente_extractor` para
  que se reconstruya contra él). También se agregó la fixture
  `event_loop_compartido` (un único asyncio event loop para toda la sesión): en
  Windows, llamar `asyncio.run()` una vez por test con los clientes httpx async
  compartidos de `gemini_flash`/`mistral_large` (singletons de módulo en `agent.py`)
  rompe con `RuntimeError: Event loop is closed` al segundo uso — no ocurre en
  producción, donde uvicorn mantiene un único loop para todo el proceso.
  `test_casos_aceptacion.py`: los 4 tests que llaman `procesar_resolucion` ahora
  pasan por `_procesar_con_reintento()`, que reintenta con espera creciente ante un
  429 (rate limit) de Mistral o Gemini — detectado por texto del error, no por tipo
  de excepción, para no acoplarse a las clases internas de cada SDK. Si la cuota
  agotada es la diaria de un tier gratuito (ej. Gemini free tier: 20 solicitudes/
  día), el reintento no ayuda y los tests seguirán fallando hasta el día siguiente;
  esto se documentó en el docstring del módulo para no confundirlo con un bug del
  pipeline.

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
