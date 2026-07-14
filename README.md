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
    └── test_smoke.py     # Prueba de humo: schemas + import de la app
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
- Ejecutar pruebas de humo:
  ```powershell
  pytest tests/
  ```
