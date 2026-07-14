# Asistente de Actualizaciones Pesqueras — MVP

MVP que monitorea resoluciones de **PRODUCE** (Perú), las clasifica y extrae con una
cascada de dos modelos (Gemini Flash → Mistral Large), y persiste las normas
estructuradas en PostgreSQL con trazabilidad completa (línea de tiempo + Dead Letter Queue).

## Estructura del proyecto

Estructura deliberadamente pequeña (aprendizaje/demo, no producción), adaptada de la
sugerida por el curso: se reemplaza Streamlit por una **web HTML estática servida por
FastAPI**, y se añaden los módulos mínimos que exige el pipeline en cascada de las
Fichas 2 y 3 (schemas, parseo de PDF, conexión a BD).

```text
resoluciones-pesqueras-mvp/
├── app.py              # FastAPI: sirve static/ + API (procesar, timeline, DLQ)
├── agent.py            # Cliente MCP + agentes LangChain (clasificador + extractor). Expone procesar_resolucion()
├── mcp_server.py        # Servidor FastMCP: 5 tools de BD (lectura, crear/actualizar/derogar norma, DLQ)
├── schemas.py           # Contratos Pydantic: ClasificacionDocumento, NormaPesquera
├── pdf_service.py       # Descarga + hash SHA-256 + extracción de texto (pdfplumber)
├── database.py          # Engine SQLAlchemy + creación idempotente del esquema
├── config.py            # Variables de entorno y configuración mínima
├── prompts.py            # System prompts del clasificador y del agente extractor
├── requirements.txt
├── .env.example
├── README.md
│
├── static/               # Interfaz web (HTML/CSS/JS estático, sin build step)
│   ├── index.html
│   ├── style.css
│   └── app.js
│
├── data/
│   └── datos_demo.csv    # Resoluciones de ejemplo (texto simulado, sin datos reales)
│
└── tests/
    ├── conftest.py               # Fixture: levanta mcp_server.py + BD Postgres descartable
    ├── test_smoke.py             # Prueba de humo: casos rápidos/mínimos/felices
    └── test_casos_aceptacion.py  # 5 casos de aceptación de la rúbrica (feliz, límite,
                                   # fuera de alcance, tool inválida, MCP no disponible)
```

### Mapeo con la estructura sugerida por el curso

| Sugerido por el curso | En este MVP | Motivo del cambio |
|---|---|---|
| `app.py` (Streamlit) | `app.py` (FastAPI) + `static/` | Se pidió web HTML estática en vez de Streamlit |
| `agent.py` | `agent.py` | Igual responsabilidad: cliente MCP + agente, expone la función de negocio (`procesar_resolucion`) |
| `mcp_server.py` | `mcp_server.py` | Igual; 5 tools en vez de 1-3 porque el dominio (Ficha 3) las requiere |
| `config.py` | `config.py` | Igual |
| `prompts.py` | `prompts.py` | Igual |
| `requirements.txt`, `.env.example`, `README.md` | Igual | Igual |
| `data/` | `data/` | `datos_demo.csv` reemplaza el texto hardcodeado del PoC |
| `tests/` | `tests/` | Igual |
| *(no estaba en la sugerencia)* | `schemas.py`, `pdf_service.py`, `database.py` | El pipeline en cascada (clasificar → extraer → persistir) necesita el contrato Pydantic, el parseo de PDF y la conexión a BD como módulos propios; mantenerlos dentro de `agent.py`/`mcp_server.py` los haría difíciles de testear por separado |

## MVP vs. PoC (Google Colab)

El PoC de referencia ([docs/PoCNormasPesqueras.ipynb](docs/PoCNormasPesqueras.ipynb)) validó
la lógica de negocio (clasificación, extracción, persistencia con política "nunca inventar")
en un notebook de Colab ejecutado celda por celda, con una BD PostgreSQL efímera instalada
dentro de la propia sesión. El MVP reutiliza esa lógica pero la convierte en software
modular real. Diferencias principales:

| Aspecto | PoC (Colab) | MVP |
|---|---|---|
| Tools del agente extractor | 5 `@tool` de LangChain definidas e invocadas en el mismo proceso Python, pasadas directo a `create_agent(tools=tools)` | Servidor **FastMCP** separado (`mcp_server.py`), expuesto por HTTP; el agente las descubre dinámicamente con `MultiServerMCPClient` — arquitectura de microservicios real, no solo funciones en memoria |
| Tool de lectura | `ejecutar_consulta_sql`: el LLM escribe SQL libre (`SELECT`/`WITH`) sobre el esquema | Reemplazada por tools estructuradas y parametrizadas (`buscar_normas`, `buscar_normas_por_resolucion`, `obtener_norma`) que validan `accion`/`estado` contra enums antes de consultar — el modelo ya no escribe SQL |
| `nro_resolucion` | Parámetro externo de `procesar_resolucion(...)`, asumido ya conocido de antemano | El propio agente extractor lo identifica del texto del documento (PASO 1 de `EXTRACCION_SYSTEM_PROMPT`); no se pide en ningún formulario de la web |
| Dead Letter Queue | Lista en memoria (`dlq: list[dict] = []`) — se pierde al reiniciar el runtime | Persistida en tabla `dlq_documentos` en PostgreSQL |
| Fallback multimodal | El clasificador solo marca `pendiente_multimodal` y el flujo se detiene ahí; la llamada a Gemini Vision queda como paso pendiente, nunca implementado en el notebook | Implementado de punta a punta: `_extraer_texto_multimodal()` envía el PDF completo a Gemini Flash, reemplaza el texto y reclasifica una vez antes de continuar al extractor |
| Base de datos | PostgreSQL instalado dentro de la sesión de Colab (`apt-get install postgresql`), efímera | PostgreSQL externo persistente, vía `DATABASE_URL` |
| Ingesta de PDF | `cargar_pdf_desde_url` + `extraer_texto_pdf` existen en el notebook, pero la demo corre sobre un `TEXTO_DEMO` hardcodeado; nunca se ejercita una descarga real | Endpoints reales (`/api/procesar/url`, `/api/procesar/pdf`) que descargan o reciben PDFs de verdad |
| Interfaz | Ninguna — notebook ejecutado celda por celda | Web real (FastAPI + HTML/JS) con 3 modos de entrada (texto/URL/PDF) y vistas de línea de tiempo + DLQ |
| Pruebas | Ninguna automatizada — "playground" manual (Sección 3 del notebook), casos ejecutados y leídos a mano | Suite de `pytest` automatizada (`test_smoke.py`, `test_casos_aceptacion.py`) con fixtures que levantan el MCP + una BD descartable |
| Observabilidad (LangSmith) | Activada por defecto (`LANGSMITH_TRACING=true`) | Opcional, apagada por defecto (`LANGSMITH_TRACING=false` en `.env.example`) |

**Qué se reutilizó del PoC casi 1:1:** los dos modelos de la cascada y sus roles (Gemini
Flash como clasificador/filtro de costos, Mistral Large como extractor), los schemas
Pydantic (`ClasificacionDocumento`, `NormaPesquera`), el esquema de base de datos
(`documentos`, `normas_actuales`, `linea_tiempo_normas`), la lógica de los 3 flujos de
escritura (creación/actualización/derogación) y la política de incertidumbre "nunca inventar".

**Qué quedó fuera del MVP** (ni el PoC ni el MVP lo implementan): monitoreo automático del
portal de PRODUCE (scraper/scheduler), distribución/notificación a usuarios finales, y
persistencia propia del PDF original (capa Raw en S3) — ver la sección
[Limitaciones](#limitaciones) para el detalle.

## Cómo levantar el MVP

1. **Instalar dependencias**
   ```powershell
   pip install -r requirements.txt
   ```
2. **Configurar variables de entorno**
   ```powershell
   Copy-Item .env.example .env
   # completar GOOGLE_API_KEY, MISTRAL_API_KEY, DATABASE_URL, etc.
   ```

   | Variable | Obligatoria | Propósito |
   |---|---|---|
   | `GOOGLE_API_KEY` | Sí | API key de Google AI Studio, para el clasificador (Gemini Flash) y el fallback multimodal |
   | `MISTRAL_API_KEY` | Sí | API key de Mistral, para el agente extractor (Mistral Large) |
   | `GEMINI_MODEL` | No (default `gemini-2.5-flash`) | Modelo de Gemini a usar en la cascada |
   | `MISTRAL_MODEL` | No (default `mistral-large-latest`) | Modelo de Mistral a usar en la cascada |
   | `LANGSMITH_TRACING` | No (default `false`) | Activa el trazado de llamadas a modelos/tools en LangSmith |
   | `LANGSMITH_PROJECT` | No | Nombre del proyecto en LangSmith (solo si `LANGSMITH_TRACING=true`) |
   | `LANGSMITH_API_KEY` | No | API key de LangSmith (solo si `LANGSMITH_TRACING=true`) |
   | `DATABASE_URL` | Sí | Cadena de conexión SQLAlchemy a PostgreSQL (`postgresql+psycopg2://usuario:clave@host:puerto/bd`) |
   | `MCP_SERVER_HOST` | No (default `127.0.0.1`) | Host donde escucha `mcp_server.py` |
   | `MCP_SERVER_PORT` | No (default `8765`) | Puerto donde escucha `mcp_server.py` |
   | `MCP_SERVER_URL` | Sí | URL completa (`http://host:puerto/mcp`) que usa `agent.py` para conectarse al servidor MCP |
   | `APP_HOST` | No (default `127.0.0.1`) | Host donde escucha la web (`app.py`) |
   | `APP_PORT` | No (default `8000`) | Puerto donde escucha la web (`app.py`) |

3. **Levantar PostgreSQL** (local o externo) y crear la base indicada en `DATABASE_URL`.
4. **Levantar el servidor MCP** (en una terminal):
   ```powershell
   python mcp_server.py
   ```
5. **Levantar la web** (en otra terminal):
   ```powershell
   python app.py
   ```
   Abrir `http://127.0.0.1:8000`.

## Evaluar el MVP

- Procesa una resolución desde la web (`http://127.0.0.1:8000`) por cualquiera de sus 3 modos:
  texto pegado, URL de un PDF de PRODUCE, o subiendo un PDF directamente (puedes usar un caso
  de [data/datos_demo.csv](data/datos_demo.csv) para el modo texto). El N° de resolución no se
  ingresa en el formulario: el agente extractor lo identifica del propio contenido del documento.
- Revisa la **línea de tiempo de normas** para confirmar que se creó/actualizó/derogó correctamente.
- Revisa la **Dead Letter Queue** para casos de confianza baja o ambigüedad legal (política de "nunca inventar").

### Guion de demo — flujo feliz de principio a fin

Todos los documentos de ejemplo usados aquí están en [data/datos_demo.csv](data/datos_demo.csv)
(texto simulado, sin datos reales); no hace falta buscar una resolución real de PRODUCE para
probar el MVP. La carpeta [data/](data) también incluye dos PDFs de ejemplo
(`ejemplo_resolucion_1.pdf`, `ejemplo_resolucion_2.pdf`) — sirven solo para verificar el formato
real de una resolución (útiles para probar la pestaña **Subir PDF**), no para los pasos con texto
de abajo. Si necesitas resoluciones oficiales reales, el repositorio público de PRODUCE está en
<https://www.gob.pe/institucion/produce/informes-publicaciones>.

1. Con el servidor MCP y la web levantados (pasos 4-5 de arriba), abrir `http://127.0.0.1:8000`.
2. En la pestaña **Texto**, pegar el texto de la fila `R.M. 067-2025-PRODUCE` (cuota de merluza)
   de `data/datos_demo.csv` y enviar. El pipeline: clasifica como relevante → el agente extractor
   identifica el N° de resolución del propio texto → no encuentra antecedente vigente para
   "merluza" → llama `crear_norma`.
3. Revisar la **línea de tiempo de normas**: debe aparecer la norma nueva con estado `vigente` y
   un único evento `creacion`.
4. Pegar ahora el texto de la fila `R.M. 234-2025-PRODUCE` (veda de anchoveta) y enviar → se crea
   una segunda norma independiente (`crear_norma`, distinta especie/objeto).
5. Pegar el texto de la fila `R.M. 289-2025-PRODUCE` ("Se levanta la veda... establecida por R.M.
   234-2025-PRODUCE") y enviar. El agente usa `buscar_normas_por_resolucion` para ubicar la norma
   creada en el paso 4 por su número de resolución, y llama `derogar_norma` sobre esa misma
   `norma_id`.
6. Revisar de nuevo la línea de tiempo: la norma de anchoveta debe mostrar ahora estado `derogada`,
   con dos eventos (`creacion` y `derogacion`) enlazados a sus respectivos documentos.
7. (Opcional, caso límite/DLQ) Pegar el texto de la fila `R.D. 456-2025-PRODUCE` (designación de
   un funcionario, sin datos pesqueros) y enviar → el clasificador lo marca como no relevante y
   se descarta sin llegar al agente extractor ni a la BD.
- Ejecutar pruebas de humo:
  ```powershell
  pytest tests/
  ```
  `test_smoke.py` corre siempre (no requiere credenciales ni Postgres real).
  `test_casos_aceptacion.py` cubre los 5 casos mínimos que exige la rúbrica del
  curso (feliz, límite, fuera de alcance, tool inválida, MCP no disponible); los
  casos que ejercitan al agente extractor completo (feliz y límite, más "fuera de
  alcance" que solo necesita el clasificador) usan la fixture `mcp_de_prueba`
  (`conftest.py`), que levanta el propio `mcp_server.py` en un hilo de fondo
  contra una base PostgreSQL descartable (`<db>_test`, recreada en cada sesión de
  pytest) — **no hace falta tener `python mcp_server.py` corriendo a mano**, solo
  un servidor PostgreSQL alcanzable (mismo host/credenciales que `DATABASE_URL`)
  y `GOOGLE_API_KEY`/`MISTRAL_API_KEY`. Se saltan automáticamente (`SKIPPED`) si
  algo de eso no está disponible.

## Limitaciones

- **Ingesta manual, sin web scraper.** El MVP no monitorea el portal de PRODUCE por sí
  mismo: el usuario pega el texto, pega la URL de un PDF o sube un PDF manualmente desde
  la web. La visión original (detectar cambios normativos automáticamente) queda fuera
  de este MVP.
- **Sin distribución ni notificación a usuarios.** Todo el resultado (línea de tiempo,
  DLQ) queda visible solo en la interfaz web, para fines de validación del MVP; no hay
  ningún canal de notificación (correo, SMS, etc.) hacia pescadores u otros usuarios finales.
- **Sin persistencia de los documentos originales.** No se guarda una copia propia del
  PDF procesado; solo se persiste su `hash_pdf` (SHA-256, para idempotencia) y, cuando el
  documento se procesó por URL, la URL fuente. Si esa URL deja de estar disponible, no hay
  forma de recuperar el documento original desde este sistema.
- **Cuota gratuita de Gemini.** El tier gratuito de Gemini limita a 20 solicitudes/día por
  modelo; puede bloquear ejecuciones repetidas de `test_casos_aceptacion.py` con LLM real
  en el mismo día.
