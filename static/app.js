// app.js — lógica de la interfaz HTML estática (sin build step, fetch a la API FastAPI)

const $ = (sel) => document.querySelector(sel);

async function procesarResolucion(payload) {
  const resp = await fetch("/api/procesar/texto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

$("#form-procesar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    nro_resolucion: $("#nro_resolucion").value,
    fecha_publicacion: $("#fecha_publicacion").value,
    texto: $("#texto").value,
  };
  mostrarResultado({ estado: "procesando..." });
  try {
    const data = await procesarResolucion(payload);
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
