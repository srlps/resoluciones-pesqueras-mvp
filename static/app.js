// app.js — lógica de la interfaz HTML estática (sin build step, fetch a la API FastAPI)

const $ = (sel) => document.querySelector(sel);

async function procesarTexto(payload) {
  const resp = await fetch("/api/procesar/texto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return resp.json();
}

async function procesarUrl(payload) {
  const resp = await fetch("/api/procesar/url", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return resp.json();
}

async function procesarPdf(formData) {
  const resp = await fetch("/api/procesar/pdf", {
    method: "POST",
    body: formData,
  });
  return resp.json();
}

function mostrarResultado(data) {
  const div = $("#resultado");
  div.textContent = JSON.stringify(data, null, 2);
  div.className = "resultado";
  if (data.estado === "procesado_por_agente") div.classList.add("ok");
  else if (data.estado === "duplicado" || data.estado === "descartado") div.classList.add("warn");
  else div.classList.add("error");
}

// ── Tabs: alterna entre los 3 modos de procesamiento (texto / url / pdf) ────
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".form-modo").forEach((form) => {
      form.hidden = form.dataset.modo !== btn.dataset.modo;
    });
  });
});

$("#form-texto").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const payload = {
    texto: form.texto.value,
    fecha_publicacion: form.fecha_publicacion.value,
  };
  mostrarResultado({ estado: "procesando..." });
  try {
    const data = await procesarTexto(payload);
    mostrarResultado(data);
    cargarNormas();
    cargarDlq();
  } catch (err) {
    mostrarResultado({ estado: "error", detalle: String(err) });
  }
});

$("#form-url").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const payload = {
    url: form.url.value,
    fecha_publicacion: form.fecha_publicacion.value,
  };
  mostrarResultado({ estado: "procesando..." });
  try {
    const data = await procesarUrl(payload);
    mostrarResultado(data);
    cargarNormas();
    cargarDlq();
  } catch (err) {
    mostrarResultado({ estado: "error", detalle: String(err) });
  }
});

$("#form-pdf").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const formData = new FormData();
  formData.append("archivo", form.archivo.files[0]);
  formData.append("fecha_publicacion", form.fecha_publicacion.value);
  mostrarResultado({ estado: "procesando..." });
  try {
    const data = await procesarPdf(formData);
    mostrarResultado(data);
    cargarNormas();
    cargarDlq();
  } catch (err) {
    mostrarResultado({ estado: "error", detalle: String(err) });
  }
});

async function cargarNormas() {
  const resp = await fetch("/api/normas");
  const normas = await resp.json();
  const tbody = $("#tabla-normas tbody");
  tbody.innerHTML = "";
  for (const n of normas) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${n.nro_resolucion ?? ""}</td>
      <td>${n.objeto ?? ""}</td>
      <td>${n.accion ?? ""}</td>
      <td>${n.lugar ?? ""}</td>
      <td>${n.estado ?? ""}</td>
      <td>${n.tipo_cambio ?? ""}</td>
      <td>${n.fecha_cambio ?? ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function cargarDlq() {
  const resp = await fetch("/api/dlq");
  const entradas = await resp.json();
  const tbody = $("#tabla-dlq tbody");
  tbody.innerHTML = "";
  for (const d of entradas) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${d.id}</td>
      <td>${d.motivo ?? ""}</td>
      <td>${(d.hash_pdf ?? "").slice(0, 16)}...</td>
      <td>${d.fecha_creacion ?? ""}</td>
      <td><button data-id="${d.id}" class="btn-revisar">Marcar revisado</button></td>
    `;
    tbody.appendChild(tr);
  }
  document.querySelectorAll(".btn-revisar").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/dlq/${btn.dataset.id}/revisar`, { method: "POST" });
      cargarDlq();
    });
  });
}

$("#btn-refrescar-normas").addEventListener("click", cargarNormas);
$("#btn-refrescar-dlq").addEventListener("click", cargarDlq);

cargarNormas();
cargarDlq();
