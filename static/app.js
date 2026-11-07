/**
 * FinderJR — Lógica de interfaz (Búsqueda + Resultados + Previsualizador + Preferencias)
 * Basada en la interfaz de cBank. Búsqueda bruta de texto completo.
 */

const SEARCH_KEYS = ["todas", "alguna", "frase", "excluye"];

const state = {
  todas: "",
  alguna: "",
  frase: "",
  excluye: "",
  sort_by: "relevancia",
  limit: 200,
  offset: 0,
  total_count: 0,
  items: [],
  terms: [],
  selectedDocId: null,
  selectedDoc: null,
  activeTab: "highlight",
  currentDocPage: 1,
  zoomPercent: 100,
  wasIndexing: false,
  // Etiquetas manuales
  etiquetas: [],
  etMap: {},
  sinEtiquetar: 0,
  filtroEt: new Set(), // deben llevar todas estas
  filtroEtx: new Set(), // no deben llevar ninguna de estas
  sinEt: false, // solo documentos sin ninguna etiqueta
  checked: new Set(), // documentos marcados con la casilla
  lastCheckIdx: null,
  renaming: false, // hay un nombre en edición
  // Fuentes de documentos (cada una con su índice y sus etiquetas)
  fuente: null, // id de la fuente con la que trabaja esta pestaña
  fuentes: [],
  folderConfigurada: true,
  remoto: false, // acceso desde Internet: solo consultar y etiquetar (el servidor lo impone igualmente)
  // Calendario (fecha de modificación): filtro "AAAA-MM-DD" (día) o "AAAA-MM" (mes) y mes que se ve
  fecha: "",
  calMes: "",
  // OCR de los escaneados (OCR de Windows, en segundo plano o con el botón)
  ocrDisponible: false,
  ocrActivo: false,
  ocrHechos: 0,
  ocrEnCurso: false, // OCR pedido con el botón
};

// ============================================================================
// MÓVIL: con pantalla estrecha se ve un panel cada vez (Búsqueda | Resultados | Documento)
// ============================================================================
const MOVIL = window.matchMedia("(max-width: 760px)");

function esMovil() {
  return MOVIL.matches;
}

function setVista(vista, historial = true) {
  if (document.body.dataset.vista === vista) return;
  document.body.dataset.vista = vista;
  document.querySelectorAll("#mobileNav button").forEach((b) => b.classList.toggle("active", b.dataset.vista === vista));
  // El botón «atrás» del móvil vuelve al panel anterior
  if (historial && esMovil()) {
    try {
      history.pushState({ vista }, "");
    } catch (e) {}
  }
  if (esMovil()) window.scrollTo(0, 0);
}

let searchDebounceTimer = null;

// ============================================================================
// UTILIDADES
// ============================================================================

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Expresión regular insensible a acentos: "credito" encuentra "crédito"
const ACCENT_CLASSES = { a: "aáàäâ", e: "eéèëê", i: "iíìïî", o: "oóòöô", u: "uúùüû", n: "nñ", c: "cç" };
function accentPattern(term) {
  return term
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .split("")
    .map((ch) => {
      const low = ch.toLowerCase();
      if (ACCENT_CLASSES[low]) return `[${ACCENT_CLASSES[low]}${ACCENT_CLASSES[low].toUpperCase()}]`;
      if (/\s/.test(ch)) return "\\s+";
      return ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    })
    .join("");
}

function highlightText(text, terms) {
  const safe = escapeHtml(text || "");
  const list = (terms || []).filter((t) => t && t.length >= 2);
  if (!list.length) return safe;
  const sorted = [...list].sort((a, b) => b.length - a.length);
  const regex = new RegExp(`(${sorted.map((t) => accentPattern(escapeHtml(t))).join("|")})`, "gi");
  return safe.replace(regex, '<mark class="hl">$1</mark>');
}

function formatDateFromEpoch(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}/${d.getFullYear()}`;
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString("es-ES");
}

// Tipo de documento por la extensión: los PDF tienen vista de página; Word y Excel, solo texto
function extDe(nombre) {
  const m = /\.([^.\\/]+)$/.exec(nombre || "");
  return m ? m[1].toLowerCase() : "";
}

function esPdf(doc) {
  return extDe(doc.nombre) === "pdf";
}

function esExcel(doc) {
  return ["xlsx", "xlsm"].includes(extDe(doc.nombre));
}

// «3 págs.» o, en un Excel, «3 hojas»
function fmtPaginas(doc, corto = true) {
  const n = doc.num_paginas || 1;
  if (esExcel(doc)) return n === 1 ? "1 hoja" : `${n} hojas`;
  return n === 1 ? "1 pág." : `${n} ${corto ? "págs." : "pág."}`;
}

// Etiqueta de tipo para las filas que no son PDF
function tipoBadge(doc) {
  const ext = extDe(doc.nombre);
  if (ext === "pdf") return "";
  const cls = esExcel(doc) ? "tipo-excel" : "tipo-word";
  return `<span class="tipo-badge ${cls}" title="Documento de ${esExcel(doc) ? "Excel" : "Word"}">${ext.toUpperCase()}</span>`;
}

function fmtSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
}

function showToast(msg, duration = 3800, action = null) {
  const c = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "toast-action";
    b.textContent = action.label;
    b.addEventListener("click", () => {
      el.remove();
      action.fn();
    });
    el.appendChild(b);
  }
  c.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

function hasSearch() {
  return SEARCH_KEYS.some((k) => state[k].trim());
}

function searchParamsObject() {
  const o = {};
  SEARCH_KEYS.forEach((k) => state[k].trim() && (o[k] = state[k].trim()));
  if (state.filtroEt.size) o.et = [...state.filtroEt].join(",");
  if (state.filtroEtx.size) o.etx = [...state.filtroEtx].join(",");
  if (state.sinEt) o.sin_et = "1";
  if (state.fecha) o.fecha = state.fecha;
  return o;
}

function hasTagFilter() {
  return state.filtroEt.size > 0 || state.filtroEtx.size > 0 || state.sinEt;
}

function buildQueryParams() {
  const p = new URLSearchParams(searchParamsObject());
  p.set("sort_by", state.sort_by);
  p.set("limit", String(state.limit));
  p.set("offset", String(state.offset));
  return p;
}

function termsQuery() {
  return state.terms.map((t) => `&t=${encodeURIComponent(t)}`).join("") + fuenteQuery();
}

// Los id de documento se repiten entre fuentes: la fuente en la URL evita que la caché del navegador
// (o el visor PDF) muestre el documento de otra fuente
function fuenteQuery() {
  return state.fuente ? `&f=${encodeURIComponent(state.fuente)}` : "";
}

// Cabeceras de las peticiones que modifican algo: el servidor las rechaza (409) si la fuente activa
// ha cambiado en otra pestaña
function postHeaders() {
  const h = { "Content-Type": "application/json" };
  if (state.fuente) h["X-FinderJR-Fuente"] = state.fuente;
  return h;
}

// ============================================================================
// INFORMACIÓN GENERAL Y BÚSQUEDA
// ============================================================================

async function loadInfo() {
  try {
    const data = await (await fetch("/api/info")).json();
    if (await aplicarFuente(data)) return; // ha cambiado en otra pestaña: ya se ha recargado todo
    state.remoto = !!data.remoto;
    document.body.classList.toggle("remoto", state.remoto);
    state.ocrDisponible = !!(data.ocr && data.ocr.disponible);
    const st = data.stats || {};
    document.getElementById("folderStatusText").textContent = !data.folder_configurada
      ? "Sin carpeta configurada: indícala en Preferencias"
      : `${data.folder_display}${data.folder_exists ? "" : " (no accesible)"} — ${fmtNum(st.total_docs)} documentos ` +
        `(${fmtNum(st.total_pages)} págs.) · ${fmtNum(st.sin_texto)} escaneados sin texto`;
    const nDup = st.duplicados || 0;
    document.getElementById("btnDuplicados").style.display = nDup ? "" : "none";
    document.getElementById("dupsCount").textContent = fmtNum(nDup);
    document.getElementById("btnDuplicados").title =
      `${fmtNum(nDup)} copias idénticas (${fmtSize(st.duplicados_bytes || 0)}): solo se indexa uno de cada grupo. Revísalas y elimina las que sobren`;
    if (data.ui_layout && !window._layoutInitializedFromDisk) {
      window._layoutInitializedFromDisk = true;
      applyPanelLayout(data.ui_layout, false);
    }
    updateIndexProgress(data.estado);
  } catch (err) {
    console.error("Error cargando información:", err);
  }
}

async function runSearch(resetOffset = true, opts = {}) {
  if (resetOffset) state.offset = 0;
  if (resetOffset && !opts.keepChecks) state.checked.clear();
  const prevIdx = state.items.findIndex((it) => it.id === state.selectedDocId);
  const tbody = document.getElementById("resultsTbody");
  try {
    const res = await fetch(`/api/search?${buildQueryParams().toString()}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.total_count = data.total_count || 0;
    state.items = data.items || [];
    state.terms = data.terms || [];

    updateCounters();

    // Los marcados que ya no aparecen dejan de estarlo
    const visibles = new Set(state.items.map((it) => it.id));
    [...state.checked].forEach((id) => !visibles.has(id) && state.checked.delete(id));

    renderGridRows();
    updateBulkUI();
    loadCalendar();
    const actual = state.items.find((it) => it.id === state.selectedDocId);
    if (state.items.length > 0 && !actual) {
      // Si el documento sale de los resultados (p. ej. al etiquetarlo con un filtro activo), pasar al siguiente
      const idx = opts.keepPreview && prevIdx >= 0 ? Math.min(prevIdx, state.items.length - 1) : 0;
      selectDocument(state.items[idx].id, !!opts.keepPreview);
    } else if (actual && opts.keepPreview) {
      if (state.selectedDoc) state.selectedDoc.etiquetas = actual.etiquetas || [];
      renderPreviewTags();
    } else if (state.selectedDoc && (state.activeTab !== "pdf" || !esPdf(state.selectedDoc))) {
      updatePreviewPane();
    }
  } catch (err) {
    console.error("Error en búsqueda:", err);
    const msg = state.remoto && err instanceof TypeError
      ? "Se ha perdido la conexión o ha caducado la sesión. Recarga la página."
      : `Error en la búsqueda: ${escapeHtml(err.message)}`;
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">${msg}</td></tr>`;
  }
}

function updateCounters() {
  document.getElementById("kpiTotalDocs").textContent = fmtNum(state.total_count);
  document.getElementById("mobileNavCount").textContent = fmtNum(state.total_count);
  const fromIdx = state.total_count === 0 ? 0 : state.offset + 1;
  const toIdx = Math.min(state.offset + state.items.length, state.total_count);
  document.getElementById("paginationInfo").textContent = `Mostrando ${fmtNum(fromIdx)}-${fmtNum(toIdx)} de ${fmtNum(state.total_count)} documentos`;
  document.getElementById("btnPrevPage").disabled = state.offset <= 0;
  document.getElementById("btnNextPage").disabled = state.offset + state.limit >= state.total_count;
}

function renderGridRows() {
  const tbody = document.getElementById("resultsTbody");
  if (!state.items.length) {
    tbody.innerHTML = `
      <tr><td colspan="4" class="empty-cell">
        No hay documentos que cumplan la búsqueda.<br/>
        <small>Prueba con menos palabras, con el comienzo de una palabra o revisa el campo «No contiene».</small>
      </td></tr>`;
    if (!state.folderConfigurada) {
      tbody.innerHTML = `
      <tr><td colspan="4" class="empty-cell">
        La fuente <b>${escapeHtml(nombreFuente())}</b> todavía no tiene carpeta de documentos.<br/>
        <button type="button" class="btn btn-sm btn-primary empty-action" id="btnConfigFuente">Configurar carpeta</button>
      </td></tr>`;
      document.getElementById("btnConfigFuente").addEventListener("click", () => openPrefsModal(state.fuente));
    }
    return;
  }
  const terms = state.terms;
  tbody.innerHTML = state.items
    .map((it) => {
      const sel = it.id === state.selectedDocId ? "selected" : "";
      const pags = fmtPaginas(it);
      const ocr = it.sin_texto ? `<span class="ocr-badge" title="Escaneado sin capa de texto: solo se encuentra por nombre y carpeta">OCR</span>` : "";
      const copias = it.copias
        ? `<span class="copias-badge" title="Hay ${it.copias} ${it.copias === 1 ? "copia idéntica" : "copias idénticas"} en otras rutas (no se muestran aparte). Clic para revisarlas">+${it.copias}</span>`
        : "";
      const snippet = it.snippet ? `<div class="snippet-match">${highlightText(it.snippet, terms)}</div>` : "";
      const dir = it.ruta_rel.replace(/[\\/][^\\/]*$/, "");
      const chk = state.checked.has(it.id);
      // Si lo buscado está en la ruta de una copia idéntica (no en la del propio documento), se enseña
      const enCopias = hasSearch() && it.otras_rutas
        ? it.otras_rutas.split("\n").map((r) => highlightText(r, terms)).filter((h) => h.includes("<mark"))
        : [];
      const copiaMatch = enCopias.length
        ? `<div class="copia-match" title="Coincide en la ruta de una copia idéntica de este documento">Copia: ${enCopias.slice(0, 2).join(" · ")}${enCopias.length > 2 ? " …" : ""}</div>`
        : "";
      return `
        <tr data-id="${it.id}" class="${sel} ${chk ? "checked" : ""}">
          <td class="cell-check"><input type="checkbox" class="row-check" ${chk ? "checked" : ""} /></td>
          <td>
            <div class="date-cell">${formatDateFromEpoch(it.mtime)}</div>
            <span class="date-sub">${pags}</span>${tipoBadge(it)}${ocr}${copias}
          </td>
          <td>
            <div class="doc-name" title="${state.remoto ? "" : "Doble clic para cambiar el nombre"}">${highlightText(it.nombre, terms)}</div>
            <div class="folder-path-movil">${highlightText(dir === it.ruta_rel ? "(raíz)" : dir, terms)}</div>
            ${tagPillsHtml(it.etiquetas)}
            ${copiaMatch}
            ${hasSearch() ? snippet : ""}
          </td>
          <td>
            <div class="folder-path" title="${escapeHtml(it.ruta_rel)}">${highlightText(dir === it.ruta_rel ? "(raíz)" : dir, terms)}</div>
          </td>
        </tr>`;
    })
    .join("");
  tbody.querySelectorAll("tr[data-id]").forEach((tr, idx) => {
    tr.addEventListener("click", (e) => {
      const id = parseInt(tr.dataset.id, 10);
      if (e.target.closest(".cell-check")) {
        e.stopPropagation();
        toggleCheck(idx, e.target.classList.contains("row-check") ? e.target.checked : !state.checked.has(id), e.shiftKey);
        return;
      }
      if (e.target.closest(".rename-box")) return;
      if (e.target.closest(".copias-badge")) return openDupsModal(id);
      if (e.ctrlKey || e.metaKey) return toggleCheck(idx, !state.checked.has(id), false);
      if (e.shiftKey && state.lastCheckIdx !== null) return toggleCheck(idx, true, true);
      selectDocument(id, false);
      if (esMovil()) setVista("documento");
    });
    tr.querySelector(".doc-name").addEventListener("dblclick", (e) => {
      e.preventDefault();
      startRename(parseInt(tr.dataset.id, 10), e.currentTarget);
    });
  });
}

// ============================================================================
// PREVISUALIZACIÓN
// ============================================================================

async function selectDocument(docId, scrollIntoView = true) {
  state.selectedDocId = docId;
  state.currentDocPage = 1;
  document.querySelectorAll("#resultsTbody tr[data-id]").forEach((tr) => {
    const on = parseInt(tr.dataset.id, 10) === docId;
    tr.classList.toggle("selected", on);
    if (on && scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  try {
    const res = await fetch(`/api/doc/${docId}`);
    if (!res.ok) return;
    const doc = await res.json();
    if (state.selectedDocId !== docId) return;
    state.selectedDoc = doc;

    document.getElementById("prevCategoria").textContent = doc.carpeta;
    document.getElementById("prevTitle").textContent = doc.nombre;
    document.getElementById("prevTitle").title = state.remoto ? doc.nombre : "Doble clic para cambiar el nombre";
    document.getElementById("prevSubtitle").textContent = [
      `Modificado ${formatDateFromEpoch(doc.mtime)}`,
      fmtPaginas(doc, false),
      doc.ruta_rel,
    ].join("  ·  ");
    renderPreviewTags();
    renderPreviewCopias();
    document.getElementById("previewToolbar").style.display = "flex";
    document.getElementById("previewPlaceholder").style.display = "none";
    document.getElementById("btnDownloadSinglePdf").href = `/api/doc/${doc.id}/pdf?download=1${fuenteQuery()}`;
    const pdf = esPdf(doc);
    const btnAbrir = document.getElementById("btnOpenNewTab");
    btnAbrir.href = `/api/doc/${doc.id}/pdf?${fuenteQuery().slice(1)}`;
    btnAbrir.style.display = pdf ? "" : "none";
    const btnApp = document.getElementById("btnOpenApp");
    btnApp.style.display = pdf ? "none" : "";
    btnApp.title = state.remoto
      ? `Descargar y abrir con ${esExcel(doc) ? "Excel" : "Word"}`
      : `Abrir con ${esExcel(doc) ? "Excel" : "Word"} (o el programa predeterminado de Windows)`;
    updateOcrButton();
    updatePreviewPane();
  } catch (err) {
    console.error("Error cargando el documento:", err);
  }
}

function updatePreviewPane() {
  const doc = state.selectedDoc;
  if (!doc) return;
  const paneHighlight = document.getElementById("paneHighlight");
  const panePdf = document.getElementById("panePdf");
  const paneText = document.getElementById("paneText");
  const bar = document.getElementById("multipageBar");
  paneHighlight.style.display = panePdf.style.display = paneText.style.display = "none";

  // Word y Excel solo tienen la vista de texto; se recuerda la pestaña elegida para el siguiente PDF
  const pdf = esPdf(doc);
  const tab = pdf ? state.activeTab : "text";
  document.querySelectorAll(".view-tabs .tab-btn").forEach((b) => {
    const soloPdf = b.dataset.tab !== "text";
    b.disabled = soloPdf && !pdf;
    b.title = b.disabled ? "Solo disponible en los PDF" : b.dataset.titulo || b.title;
    b.classList.toggle("active", b.dataset.tab === tab);
  });

  if (tab === "highlight") {
    paneHighlight.style.display = "block";
    bar.style.display = "flex";
    document.getElementById("docPageIndicator").textContent = `Página ${state.currentDocPage} de ${doc.num_paginas}`;
    document.getElementById("btnPrevDocPage").disabled = state.currentDocPage <= 1;
    document.getElementById("btnNextDocPage").disabled = state.currentDocPage >= doc.num_paginas;
    document.getElementById("highlightImg").src = `/api/doc/${doc.id}/page_image?page=${state.currentDocPage}${termsQuery()}`;
    applyZoom();
  } else if (tab === "pdf") {
    panePdf.style.display = "block";
    bar.style.display = "none";
    const iframe = document.getElementById("pdfIframe");
    const target = `/api/doc/${doc.id}/pdf?${fuenteQuery().slice(1)}`;
    if (!iframe.src.endsWith(target)) iframe.src = target;
  } else {
    paneText.style.display = "block";
    bar.style.display = "none";
    document.getElementById("docMetadataGrid").innerHTML = `
      <div class="meta-item"><strong>Archivo</strong>${escapeHtml(doc.nombre)}</div>
      <div class="meta-item"><strong>Modificado</strong>${formatDateFromEpoch(doc.mtime)}</div>
      <div class="meta-item"><strong>${esExcel(doc) ? "Hojas" : "Páginas"} / tamaño</strong>${doc.num_paginas} · ${fmtSize(doc.size)}</div>
      <div class="meta-item"><strong>Texto</strong>${textoEstado(doc)}</div>
      <div class="meta-item" style="grid-column: 1 / -1;"><strong>Ruta</strong>${escapeHtml(doc.ruta_display)}</div>`;
    const txt = doc.texto || (doc.necesita_ocr
      ? doc.ocr === 2 ? "(Documento escaneado: el OCR no ha reconocido texto; puede ser manuscrito o una imagen)"
        : "(Documento escaneado sin capa de texto: pendiente de OCR)"
      : !pdf ? "(El documento no contiene texto: puede que solo tenga imágenes)" : "");
    document.getElementById("extractedTextBox").innerHTML = highlightText(txt, state.terms);
  }
}

function applyZoom() {
  const img = document.getElementById("highlightImg");
  if (state.zoomPercent === 100) {
    img.style.maxWidth = "100%";
    img.style.width = "auto";
  } else {
    img.style.maxWidth = "none";
    img.style.width = `${state.zoomPercent}%`;
  }
  document.getElementById("btnZoomReset").textContent = `${state.zoomPercent}%`;
}

// ============================================================================
// INDEXACIÓN EN SEGUNDO PLANO
// ============================================================================

function updateIndexProgress(estado) {
  const wrap = document.getElementById("indexProgress");
  if (!estado) return;
  const o = estado.ocr;
  if (!estado.activo && o && o.activo && o.total) {
    // Sin sincronización en marcha: se muestra el avance del OCR de los escaneados
    wrap.classList.add("visible");
    document.getElementById("indexProgressText").textContent = `${o.fuente}: OCR ${fmtNum(o.hechos)}/${fmtNum(o.total)} escaneados…`;
    document.getElementById("indexProgressBar").style.width = `${Math.round((100 * o.hechos) / o.total)}%`;
    return;
  }
  if (estado.activo) {
    wrap.classList.add("visible");
    document.getElementById("indexProgressText").textContent = estado.mensaje || "Indexando…";
    const pct = estado.total ? Math.round((100 * estado.hechos) / estado.total) : 0;
    document.getElementById("indexProgressBar").style.width = `${pct}%`;
  } else {
    wrap.classList.remove("visible");
  }
}

async function pollStatus() {
  try {
    const estado = await (await fetch("/api/status")).json();
    if (estado.activa && state.fuente && estado.activa !== state.fuente && !state.renaming) return loadInfo();
    updateIndexProgress(estado);
    seguirOcr(estado.ocr);
    if (state.wasIndexing && !estado.activo && !state.renaming) {
      const u = estado.ultimo || {};
      if (u.status === "ok" && (u.added || u.updated || u.removed || u.duplicados)) {
        const dup = u.duplicados ? ` · ${fmtNum(u.duplicados)} ${u.duplicados === 1 ? "duplicado detectado" : "duplicados detectados"} (no se indexan)` : "";
        showToast(
          `${nombreFuente()} actualizada: +${fmtNum(u.added)} nuevos, ${fmtNum(u.updated)} modificados, ${fmtNum(u.removed)} eliminados${dup}.`,
          u.duplicados ? 7000 : 3800,
          u.duplicados ? { label: "Revisar", fn: () => openDupsModal() } : null
        );
        await loadInfo();
        await runSearch(false);
      }
      const ae = u.status === "ok" && u.fuente === state.fuente && u.autoetiquetas;
      if (ae && (ae.puestas || ae.quitadas)) {
        // Etiquetado automático por el nombre (archivos nuevos o renombrados, o regla cambiada en Preferencias)
        const partes = [ae.puestas && `${fmtNum(ae.puestas)} puestas`, ae.quitadas && `${fmtNum(ae.quitadas)} quitadas`].filter(Boolean);
        if (!(u.added || u.updated || u.removed || u.duplicados)) showToast(`Etiquetas automáticas: ${partes.join(", ")}.`, 3800);
        await loadEtiquetas();
        if (!(u.added || u.updated || u.removed || u.duplicados)) await runSearch(false, { keepPreview: true, keepChecks: true });
      }
    }
    state.wasIndexing = !!estado.activo;
  } catch (e) {
    /* servidor no disponible momentáneamente */
  }
}

async function triggerSync(payload = {}) {
  const data = await (
    await fetch("/api/sync", { method: "POST", headers: postHeaders(), body: JSON.stringify(payload) })
  ).json();
  if (data.fuente_cambiada) return loadInfo();
  showToast(data.started ? "Sincronización iniciada en segundo plano." : "Ya hay una sincronización en curso.");
  state.wasIndexing = true;
  updateIndexProgress(data.estado || { activo: true, mensaje: "Iniciando…" });
}

// ============================================================================
// PREFERENCIAS
// ============================================================================

function appendCatalogRow(containerId, val = "") {
  const c = document.getElementById(containerId);
  const row = document.createElement("div");
  row.className = "catalog-row";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.className = "catalog-item-input";
  inp.value = val;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "catalog-remove-btn";
  del.title = "Eliminar";
  del.innerHTML = "&times;";
  del.addEventListener("click", () => row.remove());
  row.appendChild(inp);
  row.appendChild(del);
  c.appendChild(row);
  c.scrollTop = c.scrollHeight;
}

function readCatalogList(containerId, pendingInputId) {
  const out = [];
  document.querySelectorAll(`#${containerId} .catalog-item-input`).forEach((inp) => {
    const v = inp.value.trim();
    if (v && !out.includes(v)) out.push(v);
  });
  const pend = document.getElementById(pendingInputId);
  if (pend && pend.value.trim() && !out.includes(pend.value.trim())) out.push(pend.value.trim());
  if (pend) pend.value = "";
  return out;
}

// Borradores de las fuentes mientras el modal está abierto (se puede pasar de una a otra sin perder cambios)
const prefs = { fuentes: [], originales: {}, sel: null };

function prefsLeerFormulario() {
  const f = prefs.fuentes.find((x) => x.id === prefs.sel);
  if (!f) return;
  f.nombre = document.getElementById("prefFuenteNombre").value.trim() || f.nombre;
  f.biblioteca = document.getElementById("prefBiblioteca").value.trim();
  f.include_subfolders = document.getElementById("prefIncludeSubfolders").checked;
  f.excluded_dirs = readCatalogList("excludedListContainer", "newExcludedInput");
  f.etiquetado_auto = autoLeerFormulario();
}

const AUTO_DEFECTO = { activo: false, etiqueta_factura: "Factura", empresas: [] };

function autoNormal(r) {
  r = r || AUTO_DEFECTO;
  return {
    activo: !!r.activo,
    etiqueta_factura: (r.etiqueta_factura ?? "Factura").trim(),
    empresas: (r.empresas || []).map((e) => ({ codigo: e.codigo, etiqueta: e.etiqueta || e.codigo })),
  };
}

function autoLeerFormulario() {
  const empresas = [...document.querySelectorAll("#prefAutoEmpresas .auto-et-emp")]
    .map((row) => {
      const codigo = row.querySelector(".cod").value.trim().toUpperCase();
      return { codigo, etiqueta: row.querySelector(".etq").value.trim() || codigo };
    })
    .filter((e) => e.codigo);
  return {
    activo: document.getElementById("prefAutoActivo").checked,
    etiqueta_factura: document.getElementById("prefAutoFactura").value.trim(),
    empresas,
  };
}

function autoAddEmpresa(e = { codigo: "", etiqueta: "" }) {
  const row = document.createElement("div");
  row.className = "auto-et-emp";
  row.innerHTML = `<input type="text" class="cod" maxlength="10" placeholder="TEX" value="${escapeHtml(e.codigo)}" />
    <input type="text" class="etq" maxlength="40" placeholder="Etiqueta (por defecto, el código)" value="${escapeHtml(e.etiqueta === e.codigo ? "" : e.etiqueta)}" />
    <button type="button" class="btn btn-xs btn-outline" title="Quitar esta empresa">&times;</button>`;
  row.querySelector("button").addEventListener("click", () => row.remove());
  document.getElementById("prefAutoEmpresas").appendChild(row);
  return row;
}

function autoMostrar(r) {
  r = autoNormal(r);
  document.getElementById("prefAutoActivo").checked = r.activo;
  document.getElementById("prefAutoFactura").value = r.etiqueta_factura;
  document.getElementById("prefAutoEmpresas").innerHTML = "";
  r.empresas.forEach(autoAddEmpresa);
  document.getElementById("prefAutoPreview").style.display = "none";
  autoPintarActivo();
}

function autoPintarActivo() {
  document.getElementById("prefAutoCuerpo").classList.toggle("off", !document.getElementById("prefAutoActivo").checked);
}

async function autoVistaPrevia(fid, regla) {
  return apiPost("/api/autoetiquetas/probar", { fuente_id: fid, regla });
}

function autoResumenHtml(v) {
  const etq = Object.entries(v.por_etiqueta)
    .map(([n, c]) => `<b>${escapeHtml(n)}</b> ${fmtNum(c)}`)
    .join(" · ");
  let h = `<div><b>${fmtNum(v.cumplen)}</b> de ${fmtNum(v.total)} documentos cumplen el patrón${etq ? `: ${etq}` : "."}</div>`;
  if (v.ejemplos.length) h += `<ul>${v.ejemplos.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  if (v.casi_total)
    h += `<div class="casi">${fmtNum(v.casi_total)} empiezan por un código de empresa pero <b>no</b> cumplen el patrón (no se etiquetan; si son facturas, renómbralos):</div>
      <ul class="casi">${v.casi.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}${v.casi_total > v.casi.length ? "<li>…</li>" : ""}</ul>`;
  return h;
}

function autoResumenTexto(v) {
  const etq = Object.entries(v.por_etiqueta).map(([n, c]) => `  · ${n}: ${fmtNum(c)}`).join("\n");
  let t = `${fmtNum(v.cumplen)} de ${fmtNum(v.total)} documentos cumplen el patrón.\n${etq}`;
  if (v.ejemplos.length) t += `\n\nEjemplos:\n${v.ejemplos.slice(0, 3).map((x) => "  " + x).join("\n")}`;
  if (v.casi_total) t += `\n\n${fmtNum(v.casi_total)} empiezan por un código pero no cumplen el patrón (no se etiquetan).`;
  return t;
}

async function autoProbar() {
  const box = document.getElementById("prefAutoPreview");
  box.style.display = "";
  box.textContent = "Comprobando…";
  try {
    box.innerHTML = autoResumenHtml(await autoVistaPrevia(prefs.sel, autoLeerFormulario()));
  } catch (err) {
    box.textContent = "Error: " + err.message;
  }
}

function prefsMostrarFuente(id) {
  prefsLeerFormulario();
  prefs.sel = id;
  const f = prefs.fuentes.find((x) => x.id === id);
  document.getElementById("prefFuenteBox").style.setProperty("--fuente-sel", f.color);
  document.getElementById("prefFuenteNombre").value = f.nombre;
  document.getElementById("prefBiblioteca").value = f.biblioteca || "";
  document.getElementById("prefIncludeSubfolders").checked = f.include_subfolders !== false;
  const list = document.getElementById("excludedListContainer");
  list.innerHTML = "";
  (f.excluded_dirs || []).forEach((d) => appendCatalogRow("excludedListContainer", d));
  autoMostrar(f.etiquetado_auto);
  const orig = prefs.originales[id] || {};
  const st = document.getElementById("prefFolderStatus");
  if (!f.biblioteca) st.textContent = "Indica la carpeta de esta fuente (ruta local o de red).";
  else if (f.biblioteca !== orig.biblioteca) st.textContent = "Carpeta nueva: se indexará al guardar.";
  else st.textContent = orig.existe ? `Carpeta verificada · ${fmtNum(orig.total_docs)} documentos indexados` : "Atención: la carpeta no existe o no es accesible.";
  const quitar = document.getElementById("btnQuitarFuente");
  quitar.disabled = id === state.fuente || prefs.fuentes.length <= 1;
  quitar.title = id === state.fuente ? "Es la fuente activa: cambia antes a otra para poder quitarla." : "Quitar la fuente (sus etiquetas se conservan)";
  prefsPintarFuentes();
}

function prefsPintarFuentes() {
  const c = document.getElementById("prefFuentesTabs");
  c.innerHTML =
    prefs.fuentes
      .map(
        (f) => `<button type="button" class="pref-fuente ${f.id === prefs.sel ? "sel" : ""}" data-id="${f.id}" style="--c:${f.color}">
          <span class="dot"></span>${escapeHtml(f.nombre)}${f.id === state.fuente ? ' <small>(activa)</small>' : ""}</button>`
      )
      .join("") +
    `<span class="pref-fuente-nueva"><input type="text" id="prefNuevaFuente" maxlength="30" placeholder="Nueva fuente…" />
     <button type="button" class="btn btn-sm btn-secondary" id="btnNuevaFuente">+ Añadir</button></span>`;
  c.querySelectorAll(".pref-fuente").forEach((b) => b.addEventListener("click", () => prefsMostrarFuente(b.dataset.id)));
  const inp = document.getElementById("prefNuevaFuente");
  const crear = async () => {
    const nombre = inp.value.trim();
    if (!nombre) return inp.focus();
    try {
      const f = await apiPost("/api/fuentes", { nombre });
      prefsLeerFormulario();
      prefs.fuentes.push(f);
      prefs.originales[f.id] = JSON.parse(JSON.stringify(f));
      prefsMostrarFuente(f.id);
      document.getElementById("prefBiblioteca").focus();
      renderFuenteSwitch(state.fuentes.concat([f]));
    } catch (err) {
      showToast(err.message, 6000);
    }
  };
  document.getElementById("btnNuevaFuente").addEventListener("click", crear);
  inp.addEventListener("keydown", (e) => e.key === "Enter" && (e.preventDefault(), crear()));
}

async function openPrefsModal(fid = null) {
  if (state.remoto) return; // solo en el PC (el servidor también lo rechaza)
  try {
    const [cfgData, fData] = await Promise.all([(await fetch("/api/config")).json(), (await fetch("/api/fuentes")).json()]);
    document.getElementById("prefInterval").value = (cfgData.config || {}).sync_interval_min || 10;
    prefs.fuentes = fData.items.map((f) => ({ id: f.id, nombre: f.nombre, color: f.color, biblioteca: f.biblioteca,
      include_subfolders: f.include_subfolders, excluded_dirs: f.excluded_dirs, etiquetado_auto: autoNormal(f.etiquetado_auto) }));
    prefs.originales = Object.fromEntries(fData.items.map((f) => [f.id, JSON.parse(JSON.stringify(f))]));
    prefs.sel = null;
    prefsMostrarFuente(typeof fid === "string" ? fid : fData.activa);
    document.getElementById("prefsModal").style.display = "flex";
    if (!document.getElementById("prefBiblioteca").value) document.getElementById("prefBiblioteca").focus();
  } catch (err) {
    showToast("Error al abrir las preferencias: " + err.message);
  }
}

async function quitarFuente() {
  const f = prefs.fuentes.find((x) => x.id === prefs.sel);
  if (!f) return;
  const ok = confirm(
    `¿Quitar la fuente «${f.nombre}»?\n\n` +
      "Los documentos de su carpeta no se tocan. Se borra su índice de búsqueda y se conservan sus etiquetas: " +
      "si vuelves a crear una fuente con el mismo nombre, las recuperará."
  );
  if (!ok) return;
  try {
    await apiPost(`/api/fuentes/${f.id}/borrar`, {});
    prefs.fuentes = prefs.fuentes.filter((x) => x.id !== f.id);
    prefs.sel = null;
    prefsMostrarFuente(state.fuente);
    showToast(`Fuente «${f.nombre}» quitada.`);
    loadInfo();
  } catch (err) {
    showToast(err.message, 6000);
  }
}

async function savePrefs() {
  prefsLeerFormulario();
  try {
    for (const f of prefs.fuentes) {
      const o = prefs.originales[f.id] || {};
      const cambios = {};
      ["nombre", "biblioteca", "include_subfolders"].forEach((k) => f[k] !== o[k] && (cambios[k] = f[k]));
      if (JSON.stringify(f.excluded_dirs || []) !== JSON.stringify(o.excluded_dirs || [])) cambios.excluded_dirs = f.excluded_dirs;
      const autoAntes = autoNormal(o.etiquetado_auto);
      if (JSON.stringify(f.etiquetado_auto) !== JSON.stringify(autoAntes)) {
        // Vista previa antes de aplicar la regla a todos los documentos de la fuente
        if (f.etiquetado_auto.activo) {
          const v = await autoVistaPrevia(f.id, f.etiquetado_auto);
          if (!confirm(`Etiquetado automático de «${f.nombre}»\n\n${autoResumenTexto(v)}\n\n` +
              "¿Aplicar la regla? Se pondrán estas etiquetas (y se quitarán las automáticas que ya no la cumplan). " +
              "Las etiquetas puestas a mano no se tocan.")) return;
        } else if (autoAntes.activo &&
            !confirm(`¿Desactivar el etiquetado automático de «${f.nombre}»?\n\n` +
              "Se quitarán las etiquetas que puso automáticamente. Las puestas a mano se conservan.")) return;
        cambios.etiquetado_auto = f.etiquetado_auto;
      }
      if (Object.keys(cambios).length) await apiPost(`/api/fuentes/${f.id}`, cambios);
    }
    await apiPost("/api/config", { sync_interval_min: parseInt(document.getElementById("prefInterval").value, 10) || 10 });
    document.getElementById("prefsModal").style.display = "none";
    showToast("Preferencias guardadas. Sincronizando en segundo plano…");
    state.wasIndexing = true;
    await loadInfo();
  } catch (err) {
    showToast("Error guardando preferencias: " + err.message, 6000);
  }
}

// ============================================================================
// LAYOUT
// ============================================================================

const DEFAULT_LAYOUT = { filters_px: 300, grid_fr: 1.0, preview_fr: 1.2, col_fecha_px: 82, col_origen_px: 200 };
const currentLayout = { ...DEFAULT_LAYOUT };

function applyPanelLayout(layout, persist = false) {
  const ws = document.querySelector(".workspace");
  if (!ws || !layout) return;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  if (typeof layout.filters_px === "number") currentLayout.filters_px = clamp(Math.round(layout.filters_px), 200, 560);
  if (typeof layout.grid_fr === "number") currentLayout.grid_fr = clamp(Number(layout.grid_fr.toFixed(3)), 0.25, 1.95);
  if (typeof layout.preview_fr === "number") currentLayout.preview_fr = clamp(Number(layout.preview_fr.toFixed(3)), 0.35, 1.95);
  if (typeof layout.col_fecha_px === "number") currentLayout.col_fecha_px = clamp(Math.round(layout.col_fecha_px), 60, 220);
  if (typeof layout.col_origen_px === "number") currentLayout.col_origen_px = clamp(Math.round(layout.col_origen_px), 80, 400);
  ws.style.setProperty("--filters-col", `${currentLayout.filters_px}px`);
  ws.style.setProperty("--grid-col", `${currentLayout.grid_fr}fr`);
  ws.style.setProperty("--preview-col", `${currentLayout.preview_fr}fr`);
  const root = document.documentElement.style;
  root.setProperty("--col-fecha-px", `${currentLayout.col_fecha_px}px`);
  root.setProperty("--col-origen-px", `${currentLayout.col_origen_px}px`);
  try {
    localStorage.setItem("finderjr_ui_layout", JSON.stringify(currentLayout));
  } catch (e) {}
  if (persist && !state.remoto) {
    fetch("/api/layout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(currentLayout) }).catch(() => {});
  }
}

function updateSortHeadersUI() {
  const sel = document.getElementById("sortSelect");
  if (sel.value !== state.sort_by) sel.value = state.sort_by;
  document.querySelectorAll(".sortable-th").forEach((th) => {
    const col = th.dataset.sortCol;
    const ind = th.querySelector(".sort-indicator");
    const asc = state.sort_by === `${col}_asc`;
    const desc = state.sort_by === `${col}_desc`;
    th.classList.toggle("sort-active", asc || desc);
    ind.textContent = asc ? "▲" : desc ? "▼" : "⇅";
  });
}

// ============================================================================
// EVENTOS
// ============================================================================

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const cached = localStorage.getItem("finderjr_ui_layout");
    if (cached) applyPanelLayout(JSON.parse(cached), false);
  } catch (e) {}

  // Campos de búsqueda (búsqueda en vivo con retardo; Intro busca al momento)
  document.querySelectorAll(".search-field").forEach((inp) => {
    inp.addEventListener("input", () => {
      state[inp.dataset.key] = inp.value;
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(() => runSearch(true), 320);
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(searchDebounceTimer);
        runSearch(true);
        if (esMovil()) {
          inp.blur();
          setVista("resultados");
        }
      }
    });
  });
  document.querySelectorAll("[data-clear]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const inp = document.getElementById(btn.dataset.clear);
      inp.value = "";
      state[inp.dataset.key] = "";
      runSearch(true);
      inp.focus();
    });
  });
  document.getElementById("btnResetFilters").addEventListener("click", () => {
    document.querySelectorAll(".search-field").forEach((inp) => {
      inp.value = "";
      state[inp.dataset.key] = "";
    });
    state.filtroEt.clear();
    state.filtroEtx.clear();
    state.sinEt = false;
    state.fecha = "";
    renderTagFilters();
    runSearch(true);
    document.getElementById("qTodas").focus();
  });

  // Ordenación
  let suppressNextHeaderClick = false;
  document.querySelectorAll(".sortable-th").forEach((th) => {
    th.addEventListener("click", (e) => {
      if (suppressNextHeaderClick || e.target.closest(".col-resizer")) return;
      const col = th.dataset.sortCol;
      if (col === "fecha") state.sort_by = state.sort_by === "fecha_desc" ? "fecha_asc" : "fecha_desc";
      else state.sort_by = state.sort_by === `${col}_asc` ? `${col}_desc` : `${col}_asc`;
      updateSortHeadersUI();
      runSearch(true);
    });
  });
  document.getElementById("sortSelect").addEventListener("change", (e) => {
    state.sort_by = e.target.value;
    updateSortHeadersUI();
    runSearch(true);
  });
  updateSortHeadersUI();

  // Paginación
  document.getElementById("btnPrevPage").addEventListener("click", () => {
    if (state.offset >= state.limit) {
      state.offset -= state.limit;
      runSearch(false);
    }
  });
  document.getElementById("btnNextPage").addEventListener("click", () => {
    if (state.offset + state.limit < state.total_count) {
      state.offset += state.limit;
      runSearch(false);
    }
  });

  // Pestañas y navegación del visor
  document.querySelectorAll(".view-tabs .tab-btn").forEach((btn) => {
    btn.dataset.titulo = btn.title;
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      state.activeTab = btn.dataset.tab;
      updatePreviewPane();
    });
  });
  document.getElementById("btnPrevDocPage").addEventListener("click", () => {
    if (state.currentDocPage > 1) {
      state.currentDocPage--;
      updatePreviewPane();
    }
  });
  document.getElementById("btnNextDocPage").addEventListener("click", () => {
    if (state.selectedDoc && state.currentDocPage < state.selectedDoc.num_paginas) {
      state.currentDocPage++;
      updatePreviewPane();
    }
  });
  document.getElementById("btnZoomIn").addEventListener("click", () => {
    state.zoomPercent = Math.min(220, state.zoomPercent + 20);
    applyZoom();
  });
  document.getElementById("btnZoomOut").addEventListener("click", () => {
    state.zoomPercent = Math.max(60, state.zoomPercent - 20);
    applyZoom();
  });
  document.getElementById("btnZoomReset").addEventListener("click", () => {
    state.zoomPercent = 100;
    applyZoom();
  });
  document.getElementById("btnOpenFolder").addEventListener("click", async () => {
    if (!state.selectedDocId) return;
    const r = await fetch("/api/open_location", {
      method: "POST",
      headers: postHeaders(),
      body: JSON.stringify({ doc_id: state.selectedDocId }),
    });
    if (!r.ok) showToast("No se ha podido abrir la carpeta del documento.");
  });
  document.getElementById("btnOpenApp").addEventListener("click", async () => {
    if (!state.selectedDocId) return;
    if (state.remoto) {
      // Desde fuera no se puede abrir en el PC: se descarga y lo abre el propio dispositivo
      window.location.href = `/api/doc/${state.selectedDocId}/pdf?download=1${fuenteQuery()}`;
      return;
    }
    const r = await fetch("/api/open_file", {
      method: "POST",
      headers: postHeaders(),
      body: JSON.stringify({ doc_id: state.selectedDocId }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(err.error || "No se ha podido abrir el documento.");
    }
  });

  // Teclado: ↑ / ↓ recorre, 1-9 pone/quita etiquetas, Esc cierra el menú o desmarca
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (tagPopoverOpen()) return closeTagPopover();
      if (document.getElementById("tagsModal").style.display !== "none") return closeTagsModal();
      if (deleteModalOpen()) return closeDeleteModal();
      if (dupsModalOpen()) return closeDupsModal();
      if (state.checked.size && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return clearChecks();
    }
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^Digit[1-9]$/.test(e.code) && !state.renaming && !modalAbierto()) {
      const f = state.fuentes[parseInt(e.code.slice(5), 10) - 1];
      if (f) {
        e.preventDefault();
        return cambiarFuente(f.id);
      }
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && document.activeElement.type !== "checkbox") return;
    if (deleteModalOpen() || dupsModalOpen()) return;
    if (e.key === "Delete" && state.selectedDocId && !state.renaming && !state.remoto) {
      e.preventDefault();
      return openDeleteModal();
    }
    if (e.key === "F2" && state.selectedDocId && !state.renaming && !state.remoto) {
      e.preventDefault();
      const cell = document.querySelector(`#resultsTbody tr[data-id="${state.selectedDocId}"] .doc-name`);
      return startRename(state.selectedDocId, cell || document.getElementById("prevTitle"));
    }
    if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey && !tagPopoverOpen()) {
      e.preventDefault();
      return toggleTagByKey(parseInt(e.key, 10));
    }
    if (!state.items.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    const idx = state.items.findIndex((it) => it.id === state.selectedDocId);
    const next = e.key === "ArrowDown" ? (idx < state.items.length - 1 ? idx + 1 : 0) : idx > 0 ? idx - 1 : state.items.length - 1;
    selectDocument(state.items[next].id, true);
  });

  // Barra superior
  document.getElementById("btnSync").addEventListener("click", () => triggerSync({}));
  document.getElementById("btnExportCsv").addEventListener("click", () => {
    window.location.href = `/api/export/csv?${buildQueryParams().toString()}`;
  });
  document.getElementById("btnExportPdfBundle").addEventListener("click", () => {
    if (state.total_count === 0) return showToast("No hay documentos para unir en PDF.");
    showToast(state.total_count > 300 ? "Se unirán los 300 primeros documentos según el orden actual." : "Generando PDF con los resultados…");
    window.location.href = `/api/export/pdf_bundle?${buildQueryParams().toString()}`;
  });

  // Preferencias
  const closePrefs = () => (document.getElementById("prefsModal").style.display = "none");
  document.getElementById("btnOpenPrefs").addEventListener("click", () => openPrefsModal());
  document.getElementById("btnClosePrefs").addEventListener("click", closePrefs);
  document.getElementById("btnCancelPrefs").addEventListener("click", closePrefs);
  document.getElementById("btnSavePrefs").addEventListener("click", savePrefs);
  document.getElementById("btnQuitarFuente").addEventListener("click", quitarFuente);
  document.getElementById("prefAutoActivo").addEventListener("change", autoPintarActivo);
  document.getElementById("btnAutoAddEmpresa").addEventListener("click", () => autoAddEmpresa().querySelector(".cod").focus());
  document.getElementById("btnAutoProbar").addEventListener("click", autoProbar);
  document.getElementById("btnReindexAll").addEventListener("click", () => {
    if (!confirm(`¿Volver a leer desde cero todos los documentos de «${nombreFuente()}»? Tardará unos minutos.`)) return;
    closePrefs();
    triggerSync({ force_reindex: true });
  });
  const addExcluded = () => {
    const inp = document.getElementById("newExcludedInput");
    if (inp.value.trim()) {
      appendCatalogRow("excludedListContainer", inp.value.trim());
      inp.value = "";
      inp.focus();
    }
  };
  document.getElementById("btnAddExcluded").addEventListener("click", addExcluded);
  document.getElementById("newExcludedInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addExcluded();
    }
  });

  // Separadores arrastrables de paneles
  const resizerLeft = document.getElementById("resizerLeft");
  const resizerRight = document.getElementById("resizerRight");
  const ws = document.querySelector(".workspace");
  let activeResizer = null;
  [
    [resizerLeft, "left"],
    [resizerRight, "right"],
  ].forEach(([el, side]) => {
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      activeResizer = side;
      el.classList.add("dragging");
      document.body.classList.add("is-resizing");
    });
  });
  document.addEventListener("mousemove", (e) => {
    if (!activeResizer) return;
    const rect = ws.getBoundingClientRect();
    const pad = 10;
    if (activeResizer === "left") {
      applyPanelLayout({ filters_px: e.clientX - rect.left - pad }, false);
    } else {
      const avail = rect.width - currentLayout.filters_px - pad * 2 - 16;
      if (avail <= 300) return;
      const ratio = Math.min(Math.max((e.clientX - rect.left - pad - currentLayout.filters_px - 8) / avail, 0.22), 0.78);
      applyPanelLayout({ grid_fr: ratio * 2.2, preview_fr: (1 - ratio) * 2.2 }, false);
    }
  });
  document.addEventListener("mouseup", () => {
    if (!activeResizer) return;
    activeResizer = null;
    resizerLeft.classList.remove("dragging");
    resizerRight.classList.remove("dragging");
    document.body.classList.remove("is-resizing");
    applyPanelLayout(currentLayout, true);
  });
  resizerLeft.addEventListener("dblclick", () => {
    applyPanelLayout({ filters_px: DEFAULT_LAYOUT.filters_px }, true);
    showToast("Ancho del panel de búsqueda restaurado.");
  });
  resizerRight.addEventListener("dblclick", () => {
    applyPanelLayout({ grid_fr: DEFAULT_LAYOUT.grid_fr, preview_fr: DEFAULT_LAYOUT.preview_fr }, true);
    showToast("Proporción de resultados y previsualizador restaurada.");
  });

  // Redimensionado de columnas
  let activeCol = null;
  let colStartX = 0;
  let colStartW = 0;
  document.querySelectorAll(".col-resizer").forEach((h) => {
    h.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      activeCol = { h, key: h.dataset.col };
      colStartX = e.clientX;
      colStartW = currentLayout[activeCol.key] || DEFAULT_LAYOUT[activeCol.key] || 90;
      h.classList.add("dragging");
      document.body.classList.add("is-resizing");
    });
    h.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    h.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      applyPanelLayout({ [h.dataset.col]: DEFAULT_LAYOUT[h.dataset.col] }, true);
      showToast("Ancho de columna restaurado.");
    });
  });
  document.addEventListener("mousemove", (e) => {
    if (activeCol) applyPanelLayout({ [activeCol.key]: colStartW + (e.clientX - colStartX) }, false);
  });
  document.addEventListener("mouseup", () => {
    if (!activeCol) return;
    activeCol.h.classList.remove("dragging");
    activeCol = null;
    document.body.classList.remove("is-resizing");
    suppressNextHeaderClick = true;
    setTimeout(() => (suppressNextHeaderClick = false), 80);
    applyPanelLayout(currentLayout, true);
  });

  // Latido: mantiene vivo el servidor mientras la pestaña esté abierta
  (function initLifecycleHeartbeat() {
    const clientId =
      typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : "fj_" + Math.random().toString(36).slice(2);
    const ping = () =>
      fetch("/api/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: clientId, action: "ping" }),
        keepalive: true,
      }).catch(() => {});
    const bye = () => {
      const payload = JSON.stringify({ client_id: clientId, action: "bye" });
      if (navigator.sendBeacon) navigator.sendBeacon("/api/heartbeat", new Blob([payload], { type: "application/json" }));
    };
    const tick = () => {
      ping();
      pollStatus();
    };
    ping();
    try {
      const w = new Worker(URL.createObjectURL(new Blob(["setInterval(function(){postMessage('t')},3000);"], { type: "application/javascript" })));
      w.onmessage = tick;
    } catch (e) {
      setInterval(tick, 3000);
    }
    document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && ping());
    window.addEventListener("pagehide", bye);
    window.addEventListener("beforeunload", bye);
  })();

  initTagsUI();
  initDupsUI();
  document.getElementById("btnDelete").addEventListener("click", openDeleteModal);
  document.getElementById("btnCancelDelete").addEventListener("click", closeDeleteModal);
  document.getElementById("btnCloseDelete").addEventListener("click", closeDeleteModal);
  document.getElementById("btnConfirmDelete").addEventListener("click", confirmDelete);
  document.getElementById("btnRename").addEventListener("click", () => state.selectedDocId && startRename(state.selectedDocId, document.getElementById("prevTitle")));
  document.getElementById("prevTitle").addEventListener("dblclick", (e) => {
    if (state.selectedDocId && !e.target.closest(".rename-box")) startRename(state.selectedDocId, e.currentTarget);
  });
  // Móvil: pestañas de paneles y botón «atrás» del navegador
  document.querySelectorAll("#mobileNav button").forEach((b) => b.addEventListener("click", () => setVista(b.dataset.vista)));
  window.addEventListener("popstate", (e) => setVista((e.state && e.state.vista) || "busqueda", false));
  try {
    history.replaceState({ vista: "busqueda" }, "");
  } catch (e) {}
  document.body.dataset.vista = "busqueda";
  document.querySelector('#mobileNav button[data-vista="busqueda"]').classList.add("active");

  initCalendarUI();
  document.getElementById("btnOcr").addEventListener("click", () => ocrDocumento());

  await loadInfo();
  await loadEtiquetas();
  await runSearch(true);
  pollStatus();
  if (!esMovil()) document.getElementById("qTodas").focus();
});

// ============================================================================
// ETIQUETAS (manuales: el programa nunca etiqueta por su cuenta)
// ============================================================================

async function apiPost(url, payload) {
  const res = await fetch(url, { method: "POST", headers: postHeaders(), body: JSON.stringify(payload || {}) });
  const data = await res.json().catch(() => ({}));
  if (res.status === 409 && data.fuente_cambiada) {
    await loadInfo(); // se pone al día con la fuente activa
    throw new Error("No se ha hecho nada: la fuente activa había cambiado en otra pestaña.");
  }
  if (!res.ok || data.error) throw new Error(data.error || `Error ${res.status}`);
  return data;
}

async function loadEtiquetas() {
  try {
    const data = await (await fetch("/api/etiquetas")).json();
    state.etiquetas = data.items || [];
    state.sinEtiquetar = data.sin_etiquetar || 0;
    state.paleta = data.paleta || [];
    state.etMap = Object.fromEntries(state.etiquetas.map((t) => [t.id, t]));
    // Filtros de etiquetas que ya no existen
    [state.filtroEt, state.filtroEtx].forEach((set) => [...set].forEach((id) => !state.etMap[id] && set.delete(id)));
    renderTagFilters();
    if (tagPopoverOpen()) renderTagPopover();
  } catch (err) {
    console.error("Error cargando etiquetas:", err);
  }
}

function sortedTagIds(ids) {
  return (ids || []).filter((id) => state.etMap[id]).sort((a, b) => state.etMap[a].nombre.localeCompare(state.etMap[b].nombre, "es"));
}

function tagPill(t, extra = "", auto = false) {
  const cls = auto ? "tag-pill auto" : "tag-pill";
  const title = auto ? ` title="Puesta automáticamente por el nombre del archivo (Preferencias). Si la quitas, no se vuelve a poner."` : "";
  return `<span class="${cls}" style="--tag:${t.color}" data-tag-id="${t.id}"${title}>${escapeHtml(t.nombre)}${extra}</span>`;
}

function tagPillsHtml(ids) {
  const list = sortedTagIds(ids);
  return list.length ? `<div class="row-tags">${list.map((id) => tagPill(state.etMap[id])).join("")}</div>` : "";
}

// ---- Filtros en el panel izquierdo ----

function renderTagFilters() {
  const c = document.getElementById("tagFilterList");
  if (!c) return;
  const chips = state.etiquetas.map((t) => {
    const cls = state.filtroEt.has(t.id) ? "on" : state.filtroEtx.has(t.id) ? "off" : "";
    const ayuda = cls === "on" ? "Mostrando solo los que la llevan" : cls === "off" ? "Ocultando los que la llevan" : "Clic: ver solo estos · Ctrl+clic: ocultarlos";
    const tecla = t.tecla ? ` · Tecla ${t.tecla}: poner/quitar` : "";
    return `<button type="button" class="tag-chip ${cls}" style="--tag:${t.color}" data-id="${t.id}" title="${ayuda}${tecla}">
              <span class="dot ${t.tecla ? "dot-key" : ""}">${t.tecla || ""}</span>${escapeHtml(t.nombre)}<span class="count">${fmtNum(t.total)}</span></button>`;
  });
  chips.push(`<button type="button" class="tag-chip tag-chip-none ${state.sinEt ? "on" : ""}" data-id="none" title="Documentos sin ninguna etiqueta (útil para ir clasificando)">
                Sin etiquetar<span class="count">${fmtNum(state.sinEtiquetar)}</span></button>`);
  c.innerHTML = state.etiquetas.length
    ? chips.join("")
    : `<div class="tags-empty">Aún no hay etiquetas. Crea la primera aquí debajo o desde el botón «Etiquetar».</div>`;
  c.querySelectorAll(".tag-chip").forEach((b) =>
    b.addEventListener("click", (e) => {
      if (b.dataset.id === "none") {
        state.sinEt = !state.sinEt;
      } else {
        const id = parseInt(b.dataset.id, 10);
        const excluir = e.ctrlKey || e.metaKey;
        if (excluir) {
          state.filtroEt.delete(id);
          state.filtroEtx.has(id) ? state.filtroEtx.delete(id) : state.filtroEtx.add(id);
        } else {
          state.filtroEtx.delete(id);
          state.filtroEt.has(id) ? state.filtroEt.delete(id) : state.filtroEt.add(id);
        }
      }
      renderTagFilters();
      runSearch(true);
    })
  );
}

async function createTag(nombre) {
  nombre = (nombre || "").trim();
  if (!nombre) return null;
  try {
    const t = await apiPost("/api/etiquetas", { nombre });
    await loadEtiquetas();
    return t;
  } catch (err) {
    showToast("No se ha podido crear la etiqueta: " + err.message);
    return null;
  }
}

// ---- Marcado de documentos (casillas) ----

function toggleCheck(idx, on, range) {
  const ids = state.items.map((it) => it.id);
  const from = range && state.lastCheckIdx !== null ? Math.min(state.lastCheckIdx, idx) : idx;
  const to = range && state.lastCheckIdx !== null ? Math.max(state.lastCheckIdx, idx) : idx;
  for (let i = from; i <= to; i++) on ? state.checked.add(ids[i]) : state.checked.delete(ids[i]);
  state.lastCheckIdx = idx;
  syncChecksDom();
}

function clearChecks() {
  state.checked.clear();
  state.lastCheckIdx = null;
  syncChecksDom();
}

function syncChecksDom() {
  document.querySelectorAll("#resultsTbody tr[data-id]").forEach((tr) => {
    const on = state.checked.has(parseInt(tr.dataset.id, 10));
    tr.classList.toggle("checked", on);
    const cb = tr.querySelector(".row-check");
    if (cb) cb.checked = on;
  });
  updateBulkUI();
}

function updateBulkUI() {
  const n = state.checked.size;
  document.getElementById("bulkInfo").style.display = n ? "flex" : "none";
  document.getElementById("bulkInfoText").textContent = `${fmtNum(n)} marcado${n === 1 ? "" : "s"}`;
  document.getElementById("btnTagMenuText").textContent = n ? `Etiquetar marcados` : `Etiquetar resultados`;
  const all = document.getElementById("chkAll");
  all.checked = n > 0 && state.items.length > 0 && state.items.every((it) => state.checked.has(it.id));
  all.indeterminate = n > 0 && !all.checked;
}

// ---- Aplicar etiquetas ----

/** Destino de una acción: documentos marcados, documento actual o todos los resultados. */
function currentTarget(kind) {
  if (kind === "doc") return state.selectedDocId ? { doc_ids: [state.selectedDocId], n: 1, label: "este documento" } : null;
  if (state.checked.size) return { doc_ids: [...state.checked], n: state.checked.size, label: `${fmtNum(state.checked.size)} marcados` };
  return { busqueda: searchParamsObject(), n: state.total_count, label: `los ${fmtNum(state.total_count)} resultados` };
}

function tagsOfDoc(id) {
  if (state.selectedDoc && state.selectedDoc.id === id) return state.selectedDoc.etiquetas || [];
  const it = state.items.find((x) => x.id === id);
  return (it && it.etiquetas) || [];
}

/** ¿Llevan la etiqueta todos los documentos del destino? (null si no se puede saber: búsqueda completa) */
function targetHasTag(target, tagId) {
  if (!target.doc_ids) return null;
  return target.doc_ids.every((id) => tagsOfDoc(id).includes(tagId));
}

async function applyTag(tagId, poner, target) {
  const t = state.etMap[tagId];
  if (!t || !target || !target.n) return;
  if (target.busqueda && poner && target.n > 25 &&
      !confirm(`¿Poner la etiqueta «${t.nombre}» a los ${fmtNum(target.n)} documentos de la búsqueda actual?`)) return;
  if (target.busqueda && !poner &&
      !confirm(`¿Quitar la etiqueta «${t.nombre}» a los ${fmtNum(target.n)} documentos de la búsqueda actual?`)) return;
  try {
    const payload = { etiqueta_id: tagId, accion: poner ? "poner" : "quitar" };
    if (target.busqueda) payload.busqueda = target.busqueda;
    else payload.doc_ids = target.doc_ids;
    const r = await apiPost("/api/etiquetar", payload);
    // Actualización inmediata en pantalla
    const ids = target.doc_ids || state.items.map((it) => it.id);
    ids.forEach((id) => {
      [state.items.find((x) => x.id === id), state.selectedDoc && state.selectedDoc.id === id ? state.selectedDoc : null].forEach((o) => {
        if (!o) return;
        const set = new Set(o.etiquetas || []);
        poner ? set.add(tagId) : set.delete(tagId);
        o.etiquetas = [...set];
        // Puesta o quitada a mano: deja de ser automática
        if (o.etiquetas_auto) o.etiquetas_auto = o.etiquetas_auto.filter((x) => x !== tagId);
      });
    });
    const quien = target.n === 1 ? "1 documento" : `${fmtNum(target.n)} documentos`;
    showToast(`«${t.nombre}» ${poner ? "puesta a" : "quitada de"} ${quien}${r.cambiados !== target.n ? ` (${fmtNum(r.cambiados)} cambiados)` : ""}.`, 2600);
    await loadEtiquetas();
    if (hasTagFilter()) {
      await runSearch(false, { keepPreview: true, keepChecks: true });
    } else {
      renderGridRows();
      renderPreviewTags();
    }
    if (tagPopoverOpen()) renderTagPopover();
  } catch (err) {
    showToast("No se ha podido etiquetar: " + err.message);
  }
}

function toggleTagByKey(n) {
  const t = state.etiquetas.find((x) => x.tecla === n);
  if (!t) return showToast(`No hay ninguna etiqueta asignada a la tecla ${n} (se asigna en «Gestionar»).`, 2600);
  const target = state.checked.size ? currentTarget("bulk") : currentTarget("doc");
  if (!target) return;
  applyTag(t.id, !targetHasTag(target, t.id), target);
}

// ---- Etiquetas del documento en la previsualización ----

function renderPreviewTags() {
  const c = document.getElementById("prevTags");
  const doc = state.selectedDoc;
  if (!doc) return (c.style.display = "none");
  c.style.display = "flex";
  c.innerHTML =
    sortedTagIds(doc.etiquetas)
      .map((id) => tagPill(state.etMap[id], `<button type="button" class="tag-x" title="Quitar etiqueta">&times;</button>`, (doc.etiquetas_auto || []).includes(id)))
      .join("") +
    `<button type="button" class="tag-add" id="btnPrevAddTag" title="Poner o quitar etiquetas a este documento (o usa las teclas 1-9)">+ Etiqueta</button>`;
  c.querySelectorAll(".tag-x").forEach((x) =>
    x.addEventListener("click", () => applyTag(parseInt(x.parentElement.dataset.tagId, 10), false, currentTarget("doc")))
  );
  document.getElementById("btnPrevAddTag").addEventListener("click", (e) => openTagPopover("doc", e.currentTarget));
}

// ---- Menú emergente (poner/quitar, buscar y crear) ----

const popover = { kind: null, anchor: null };

function tagPopoverOpen() {
  return document.getElementById("tagPopover").style.display !== "none";
}

function openTagPopover(kind, anchor) {
  popover.kind = kind;
  popover.anchor = anchor;
  const el = document.getElementById("tagPopover");
  el.style.display = "flex";
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  el.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.left))}px`;
  const top = r.bottom + 4;
  el.style.top = `${Math.min(top, window.innerHeight - el.offsetHeight - 8)}px`;
  const inp = document.getElementById("tagPopoverInput");
  inp.value = "";
  renderTagPopover();
  inp.focus();
}

function closeTagPopover() {
  document.getElementById("tagPopover").style.display = "none";
}

function renderTagPopover() {
  const target = currentTarget(popover.kind);
  const list = document.getElementById("tagPopoverList");
  if (!target || !target.n) {
    document.getElementById("tagPopoverTitle").textContent = "No hay documentos a los que aplicar etiquetas";
    list.innerHTML = "";
    return;
  }
  document.getElementById("tagPopoverTitle").textContent = `Etiquetas de ${target.label}`;
  const q = document.getElementById("tagPopoverInput").value.trim();
  const qf = q.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const tags = state.etiquetas.filter((t) => !qf || t.nombre.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().includes(qf));
  const exact = state.etiquetas.some((t) => t.nombre.toLowerCase() === q.toLowerCase());
  let html = tags
    .map((t) => {
      const has = targetHasTag(target, t.id);
      const key = t.tecla ? `<kbd>${t.tecla}</kbd>` : "";
      if (has !== null) {
        // Documentos concretos: casilla que alterna
        return `<button type="button" class="tp-row tp-toggle ${has ? "on" : ""}" data-id="${t.id}" data-poner="${has ? 0 : 1}">
                  <span class="tp-check">${has ? "✓" : ""}</span>${tagPill(t)}${key}</button>`;
      }
      // Búsqueda completa: poner o quitar explícitamente
      return `<div class="tp-row">${tagPill(t)}${key}
                <span class="tp-actions">
                  <button type="button" class="btn btn-xs" data-id="${t.id}" data-poner="1">Poner</button>
                  <button type="button" class="btn btn-xs" data-id="${t.id}" data-poner="0">Quitar</button>
                </span></div>`;
    })
    .join("");
  if (q && !exact) html += `<button type="button" class="tp-row tp-create" data-create="1">+ Crear y poner «${escapeHtml(q)}»</button>`;
  if (!html) html = `<div class="tags-empty">Escribe un nombre para crear la primera etiqueta.</div>`;
  list.innerHTML = html;
  list.querySelectorAll("[data-poner]").forEach((b) =>
    b.addEventListener("click", () => applyTag(parseInt(b.dataset.id, 10), b.dataset.poner === "1", currentTarget(popover.kind)))
  );
  const cr = list.querySelector("[data-create]");
  if (cr) cr.addEventListener("click", createFromPopover);
}

async function createFromPopover() {
  const inp = document.getElementById("tagPopoverInput");
  const t = await createTag(inp.value);
  if (!t) return;
  inp.value = "";
  await applyTag(t.id, true, currentTarget(popover.kind));
  renderTagPopover();
}

// ---- Gestor (renombrar, color, tecla, borrar) ----

function openTagsModal() {
  if (state.remoto) return; // solo en el PC (el servidor también lo rechaza)
  renderTagsManage();
  document.getElementById("tagsModal").style.display = "flex";
}

function closeTagsModal() {
  document.getElementById("tagsModal").style.display = "none";
}

function renderTagsManage() {
  const c = document.getElementById("tagsManageList");
  if (!state.etiquetas.length) {
    c.innerHTML = `<div class="tags-empty">Aún no hay etiquetas.</div>`;
    return;
  }
  const paleta = state.paleta || [];
  c.innerHTML = state.etiquetas
    .map(
      (t) => `
      <div class="tm-row" data-id="${t.id}">
        <input type="text" class="tm-name" maxlength="40" value="${escapeHtml(t.nombre)}" />
        <div class="tm-colors">${paleta
          .map((col) => `<button type="button" class="tm-color ${col === t.color ? "on" : ""}" style="--tag:${col}" data-color="${col}" title="Cambiar color"></button>`)
          .join("")}</div>
        <select class="tm-key" title="Tecla rápida">
          <option value="">Tecla —</option>
          ${[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => `<option value="${n}" ${t.tecla === n ? "selected" : ""}>Tecla ${n}</option>`).join("")}
        </select>
        <span class="tm-count" title="Documentos con esta etiqueta">${fmtNum(t.total)} doc.</span>
        <button type="button" class="catalog-remove-btn tm-del" title="Borrar etiqueta">&times;</button>
      </div>`
    )
    .join("");
  c.querySelectorAll(".tm-row").forEach((row) => {
    const id = parseInt(row.dataset.id, 10);
    const t = state.etMap[id];
    const save = async (datos) => {
      try {
        await apiPost(`/api/etiquetas/${id}`, datos);
      } catch (err) {
        showToast(err.message);
      }
      await loadEtiquetas();
      renderTagsManage();
      renderGridRows();
      renderPreviewTags();
    };
    const name = row.querySelector(".tm-name");
    name.addEventListener("change", () => name.value.trim() && name.value.trim() !== t.nombre && save({ nombre: name.value.trim() }));
    name.addEventListener("keydown", (e) => e.key === "Enter" && name.blur());
    row.querySelectorAll(".tm-color").forEach((b) => b.addEventListener("click", () => save({ color: b.dataset.color })));
    row.querySelector(".tm-key").addEventListener("change", (e) => save({ tecla: e.target.value }));
    row.querySelector(".tm-del").addEventListener("click", async () => {
      const aviso = t.total ? `La llevan ${fmtNum(t.total)} documentos; se quitará de todos.` : "No la lleva ningún documento.";
      if (!confirm(`¿Borrar la etiqueta «${t.nombre}»?\n${aviso}`)) return;
      try {
        await apiPost(`/api/etiquetas/${id}/borrar`, {});
      } catch (err) {
        return showToast(err.message);
      }
      state.filtroEt.delete(id);
      state.filtroEtx.delete(id);
      await loadEtiquetas();
      renderTagsManage();
      runSearch(false, { keepPreview: true, keepChecks: true });
    });
  });
}

function initTagsUI() {
  const newInp = document.getElementById("newTagInput");
  const crear = async () => {
    if (await createTag(newInp.value)) {
      newInp.value = "";
      showToast("Etiqueta creada. Ponla con el botón «Etiquetar», con «+ Etiqueta» o con su tecla.", 3200);
    }
  };
  document.getElementById("btnNewTag").addEventListener("click", crear);
  newInp.addEventListener("keydown", (e) => e.key === "Enter" && (e.preventDefault(), crear()));

  document.getElementById("btnTagMenu").addEventListener("click", (e) => {
    e.stopPropagation();
    tagPopoverOpen() && popover.kind === "bulk" ? closeTagPopover() : openTagPopover("bulk", e.currentTarget);
  });
  document.getElementById("tagPopoverInput").addEventListener("input", renderTagPopover);
  document.getElementById("tagPopoverInput").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const q = e.target.value.trim().toLowerCase();
    if (!q) return;
    const t = state.etiquetas.find((x) => x.nombre.toLowerCase() === q);
    if (!t) return createFromPopover();
    const target = currentTarget(popover.kind);
    const has = targetHasTag(target, t.id);
    applyTag(t.id, has === null ? true : !has, target);
    e.target.value = "";
  });
  document.addEventListener("mousedown", (e) => {
    if (!tagPopoverOpen()) return;
    if (e.target.closest("#tagPopover") || e.target.closest("#btnTagMenu") || e.target.closest("#btnPrevAddTag")) return;
    closeTagPopover();
  });

  document.getElementById("chkAll").addEventListener("change", (e) => {
    state.items.forEach((it) => (e.target.checked ? state.checked.add(it.id) : state.checked.delete(it.id)));
    syncChecksDom();
  });
  document.getElementById("btnClearChecks").addEventListener("click", clearChecks);

  document.getElementById("btnManageTags").addEventListener("click", openTagsModal);
  document.getElementById("btnCloseTags").addEventListener("click", closeTagsModal);
  document.getElementById("btnDoneTags").addEventListener("click", closeTagsModal);
}

// ============================================================================
// RENOMBRADO DE ARCHIVOS (se aplica en disco; la extensión no se toca)
// ============================================================================

function splitName(nombre) {
  const i = nombre.lastIndexOf(".");
  return i > 0 ? [nombre.slice(0, i), nombre.slice(i)] : [nombre, ""];
}

/** Sustituye el contenido de `host` por un campo de edición del nombre (sin extensión). */
function startRename(docId, host) {
  if (state.remoto) return; // solo en el PC (el servidor también lo rechaza)
  if (state.renaming || !host) return;
  const it = state.items.find((x) => x.id === docId);
  const nombre = (it && it.nombre) || (state.selectedDoc && state.selectedDoc.id === docId && state.selectedDoc.nombre);
  if (!nombre) return;
  const [base, ext] = splitName(nombre);
  state.renaming = true;
  const original = host.innerHTML;
  host.innerHTML = `<span class="rename-box"><input type="text" class="rename-input" spellcheck="false" /><span class="rename-ext">${escapeHtml(ext)}</span></span>`;
  const inp = host.querySelector(".rename-input");
  inp.value = base;
  inp.focus();
  inp.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const nuevo = inp.value.trim();
    if (!save || !nuevo || nuevo === base) {
      host.innerHTML = original;
      state.renaming = false;
      return;
    }
    inp.disabled = true;
    const ok = await renameDoc(docId, nuevo, base);
    state.renaming = false;
    if (!ok) host.innerHTML = original;
  };
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  });
  inp.addEventListener("blur", () => finish(true));
  ["click", "dblclick", "mousedown"].forEach((ev) => inp.addEventListener(ev, (e) => e.stopPropagation()));
}

/** Llama al servidor; si va bien, refleja el cambio en pantalla y ofrece deshacer. */
async function renameDoc(docId, nuevaBase, baseAnterior, esDeshacer = false) {
  try {
    const r = await apiPost(`/api/doc/${docId}/renombrar`, { nombre: nuevaBase });
    applyRenamed(r.doc);
    if (r.cambiado) {
      if (esDeshacer) showToast(`Nombre restaurado: «${r.nombre}».`, 3000);
      else
        showToast(`Renombrado a «${r.nombre}».`, 7000, {
          label: "Deshacer",
          fn: () => renameDoc(docId, baseAnterior, nuevaBase, true),
        });
    }
    return true;
  } catch (err) {
    showToast(err.message, 6000);
    return false;
  }
}

function applyRenamed(doc) {
  if (!doc) return;
  const it = state.items.find((x) => x.id === doc.id);
  if (it) {
    it.nombre = doc.nombre;
    it.ruta_rel = doc.ruta_rel;
  }
  renderGridRows();
  if (state.selectedDoc && state.selectedDoc.id === doc.id) {
    Object.assign(state.selectedDoc, doc);
    document.getElementById("prevTitle").textContent = doc.nombre;
    document.getElementById("prevSubtitle").textContent = [
      `Modificado ${formatDateFromEpoch(doc.mtime)}`,
      fmtPaginas(state.selectedDoc, false),
      doc.ruta_rel,
    ].join("  ·  ");
    if (state.activeTab === "text" || !esPdf(state.selectedDoc)) updatePreviewPane();
  }
}

// ============================================================================
// ELIMINACIÓN (a la Papelera de reciclaje de Windows, con confirmación)
// ============================================================================

function deleteModalOpen() {
  return document.getElementById("deleteModal").style.display !== "none";
}

function openDeleteModal() {
  if (state.remoto) return; // solo en el PC (el servidor también lo rechaza)
  const doc = state.selectedDoc;
  if (!doc || doc.id !== state.selectedDocId) return;
  closeTagPopover();
  document.getElementById("deleteName").textContent = doc.nombre;
  document.getElementById("deletePath").textContent = doc.ruta_display || doc.ruta_rel;
  document.getElementById("deleteTags").innerHTML = tagPillsHtml(doc.etiquetas);
  const n = (doc.copias || []).length;
  document.getElementById("deleteCopiasBox").style.display = n ? "" : "none";
  document.getElementById("deleteCopias").checked = false;
  document.getElementById("deleteCopiasN").textContent = n === 1 ? "1 copia idéntica" : `${n} copias idénticas`;
  document.getElementById("deleteCopiasHelp").textContent = n
    ? `Si no la marcas, el documento sigue en FinderJR (con sus etiquetas) en la ruta de la copia: ${doc.copias[0].ruta_display}` +
      (n > 1 ? ` y ${n - 1} más.` : ".")
    : "";
  const btn = document.getElementById("btnConfirmDelete");
  btn.disabled = false;
  document.getElementById("deleteModal").style.display = "flex";
  document.getElementById("btnCancelDelete").focus(); // por defecto, lo seguro
}

function closeDeleteModal() {
  document.getElementById("deleteModal").style.display = "none";
}

async function confirmDelete() {
  const doc = state.selectedDoc;
  if (!doc) return closeDeleteModal();
  const btn = document.getElementById("btnConfirmDelete");
  btn.disabled = true;
  try {
    const r = await apiPost(`/api/doc/${doc.id}/eliminar`, { copias: document.getElementById("deleteCopias").checked });
    closeDeleteModal();
    if (r.conservado) {
      // Tenía copias: el documento sigue, ahora en la ruta de una de ellas
      showToast(`«${r.nombre}» enviado a la Papelera. El documento sigue en FinderJR con su copia: ${r.conservado}`, 7000);
      await runSearch(false, { keepPreview: true });
      selectDocument(doc.id, false);
      loadInfo();
      loadEtiquetas();
      return;
    }
    // Quitarlo de la lista y pasar al siguiente
    const idx = state.items.findIndex((x) => x.id === doc.id);
    if (idx >= 0) state.items.splice(idx, 1);
    state.checked.delete(doc.id);
    state.total_count = Math.max(0, state.total_count - 1);
    updateCounters();
    state.selectedDoc = null;
    state.selectedDocId = null;
    renderGridRows();
    updateBulkUI();
    if (state.items.length) {
      selectDocument(state.items[Math.min(Math.max(idx, 0), state.items.length - 1)].id, true);
    } else {
      document.getElementById("previewToolbar").style.display = "none";
      document.getElementById("prevTags").style.display = "none";
      document.getElementById("prevTitle").textContent = "No hay más documentos en los resultados";
      document.getElementById("prevSubtitle").textContent = "";
    }
    const extra = r.copias_eliminadas ? ` junto con ${r.copias_eliminadas} ${r.copias_eliminadas === 1 ? "copia" : "copias"}` : "";
    showToast(`«${r.nombre}» enviado a la Papelera de reciclaje${extra}.`, 5000);
    loadInfo();
    loadEtiquetas();
  } catch (err) {
    btn.disabled = false;
    showToast(err.message, 7000);
  }
}

// ============================================================================
// FUENTES DE DOCUMENTOS (Personal, Profesional…: índices y etiquetas aislados)
// ============================================================================

function nombreFuente() {
  const f = state.fuentes.find((x) => x.id === state.fuente);
  return f ? f.nombre : "Biblioteca";
}

function modalAbierto() {
  return [...document.querySelectorAll(".modal-backdrop")].some((m) => m.style.display !== "none");
}

function renderFuenteSwitch(fuentes) {
  state.fuentes = fuentes || state.fuentes;
  const c = document.getElementById("fuenteSwitch");
  c.style.display = state.fuentes.length > 1 ? "" : "none";
  c.innerHTML = state.fuentes
    .map((f, i) => {
      const on = f.id === state.fuente;
      return `<button type="button" role="tab" aria-selected="${on}" class="fuente-btn ${on ? "active" : ""}" data-id="${f.id}"
        style="--c:${f.color}" title="${escapeHtml(f.nombre)} (Alt+${i + 1})">${escapeHtml(f.nombre)}</button>`;
    })
    .join("");
  c.querySelectorAll(".fuente-btn").forEach((b) => b.addEventListener("click", () => cambiarFuente(b.dataset.id)));
}

// Aplica la fuente que indica el servidor (/api/info). Devuelve true si era otra distinta y se ha recargado todo.
async function aplicarFuente(info) {
  if (!info || !info.fuente) return false;
  const f = info.fuente;
  const cambia = state.fuente !== null && state.fuente !== f.id;
  const primera = state.fuente === null;
  state.fuente = f.id;
  state.folderConfigurada = info.folder_configurada !== false;
  document.documentElement.style.setProperty("--fuente", f.color || "#2563eb");
  document.querySelector(".brand-title").textContent = `FinderJR · ${f.nombre}`;
  document.title = `FinderJR · ${f.nombre}`;
  renderFuenteSwitch(info.fuentes);
  if (!cambia) return false;
  if (!primera) showToast(`Fuente activa: ${f.nombre} (se cambió en otra pestaña).`, 5000);
  await entrarEnFuente(info);
  return true;
}

async function cambiarFuente(id) {
  if (id === state.fuente || state.renaming) return;
  try {
    const info = await apiPost("/api/fuente/activar", { id });
    state.fuente = info.fuente.id;
    await entrarEnFuente(info);
  } catch (err) {
    showToast(err.message, 6000);
  }
}

// Todo lo que pertenece a la fuente anterior (filtros de etiqueta, marcados, documento abierto) se descarta;
// los términos de búsqueda se conservan para poder repetir la misma búsqueda en la otra fuente.
async function entrarEnFuente(info) {
  state.fuente = null; // para que aplicarFuente no lo trate como un cambio externo
  await aplicarFuente(info);
  state.filtroEt.clear();
  state.filtroEtx.clear();
  state.sinEt = false;
  state.checked.clear();
  state.lastCheckIdx = null;
  closeTagPopover();
  resetPreview();
  await loadInfo();
  await loadEtiquetas();
  await runSearch(true);
}

function resetPreview() {
  state.selectedDocId = null;
  state.selectedDoc = null;
  state.currentDocPage = 1;
  document.getElementById("previewToolbar").style.display = "none";
  document.getElementById("prevTags").style.display = "none";
  document.getElementById("multipageBar").style.display = "none";
  ["paneHighlight", "panePdf", "paneText"].forEach((id) => (document.getElementById(id).style.display = "none"));
  document.getElementById("pdfIframe").src = "about:blank";
  document.getElementById("highlightImg").removeAttribute("src");
  document.getElementById("previewPlaceholder").style.display = "";
  document.getElementById("prevCategoria").textContent = "Selecciona un documento";
  document.getElementById("prevTitle").textContent = "Haz clic en cualquier fila del listado para previsualizar el documento";
  document.getElementById("prevTitle").title = "";
  document.getElementById("prevSubtitle").textContent = "Puedes usar las teclas ↑ y ↓ del teclado para recorrer los resultados rápidamente.";
}

// ============================================================================
// DUPLICADOS (archivos idénticos byte a byte: se indexa uno por grupo; el usuario decide cuál conservar)
// ============================================================================

const dups = { grupos: [], keep: new Map(), copias: 0, bytes: 0, busy: false, foco: null };

function dupsModalOpen() {
  return document.getElementById("dupsModal").style.display !== "none";
}

function renderPreviewCopias() {
  const el = document.getElementById("prevCopias");
  const n = (state.selectedDoc && state.selectedDoc.copias ? state.selectedDoc.copias : []).length;
  el.style.display = n ? "" : "none";
  if (n) el.innerHTML = `<svg class="icon"><use href="#ico-copy"></use></svg> ${n === 1 ? "1 copia idéntica" : `${n} copias idénticas`}`;
}

// focoDocId: abre el modal con el grupo de ese documento a la vista
async function openDupsModal(focoDocId = null) {
  if (state.remoto) return; // solo en el PC (el servidor también lo rechaza)
  closeTagPopover();
  document.getElementById("dupsModal").style.display = "flex";
  dups.foco = null;
  await loadDups();
  const g = focoDocId && dups.grupos.find((x) => x.miembros.some((m) => m.doc_id === focoDocId));
  if (g) {
    dups.foco = g.principal;
    renderDups();
    const el = document.querySelector(`.dups-group[data-g="${g.principal}"]`);
    if (el) el.scrollIntoView({ block: "center" });
  } else {
    document.querySelector("#dupsModal .modal-body").scrollTop = 0;
  }
}

function closeDupsModal() {
  document.getElementById("dupsModal").style.display = "none";
  // Lo visto deja de salir como «nuevo» la próxima vez
  apiPost("/api/duplicados/visto", {}).catch(() => {});
}

async function loadDups() {
  try {
    const data = await (await fetch("/api/duplicados")).json();
    if (data.error) throw new Error(data.error);
    dups.grupos = data.grupos || [];
    dups.copias = data.copias || 0;
    dups.bytes = data.bytes || 0;
    // Se respeta lo que el usuario ya había marcado; si no, la indexada (la que tiene etiquetas o la más antigua)
    const keep = new Map();
    dups.grupos.forEach((g) => {
      const prev = dups.keep.get(g.principal);
      keep.set(g.principal, g.miembros.some((m) => m.id === prev) ? prev : g.miembros.find((m) => m.principal).id);
    });
    dups.keep = keep;
    renderDups();
  } catch (err) {
    document.getElementById("dupsList").innerHTML = `<div class="dups-empty">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function renderDups() {
  const n = dups.grupos.length;
  document.getElementById("dupsResumen").textContent = n
    ? `${fmtNum(n)} ${n === 1 ? "grupo" : "grupos"} · ${fmtNum(dups.copias)} ${dups.copias === 1 ? "copia sobrante" : "copias sobrantes"} · ${fmtSize(dups.bytes)}`
    : "Sin duplicados";
  const btnTodos = document.getElementById("btnDupsTodos");
  btnTodos.style.display = n > 1 ? "" : "none";
  btnTodos.disabled = dups.busy;
  const list = document.getElementById("dupsList");
  if (!n) {
    list.innerHTML = `<div class="dups-empty">No hay archivos duplicados en <b>${escapeHtml(nombreFuente())}</b>.</div>`;
    return;
  }
  list.innerHTML = dups.grupos
    .map((g, i) => {
      const keep = dups.keep.get(g.principal);
      const sobran = g.miembros.length - 1;
      const filas = g.miembros
        .map((m) => {
          const dir = m.ruta_rel.replace(/[\\/][^\\/]*$/, "");
          const marcas =
            (m.principal ? `<span class="dups-tag indexado" title="Es la que aparece en los resultados">indexado</span>` : "") +
            (m.nuevo ? `<span class="dups-tag nuevo" title="Detectado desde la última vez que revisaste los duplicados">nuevo</span>` : "");
          return `
          <div class="dups-member ${m.id === keep ? "keep" : "drop"}" data-id="${m.id}" title="${m.id === keep ? "Se conserva" : "Clic para conservar este en lugar del marcado"}">
            <input type="radio" name="keep-${g.principal}" ${m.id === keep ? "checked" : ""} aria-label="Conservar este" />
            <div>
              <div class="dups-name">${escapeHtml(m.nombre)}${marcas}</div>
              <div class="dups-path">${escapeHtml(dir === m.ruta_rel ? "(raíz)" : dir)}</div>
              ${tagPillsHtml(m.etiquetas)}
            </div>
            <div class="dups-destino">${m.id === keep ? "Se conserva" : "A la Papelera"}</div>
            <div class="dups-date" title="Fecha de modificación">${formatDateFromEpoch(m.mtime)}</div>
            <div class="dups-actions">
              <a class="btn btn-xs" href="/api/archivo/${m.id}/pdf?${fuenteQuery().slice(1)}" target="_blank" title="Abrir en una pestaña nueva"><svg class="icon"><use href="#ico-external-link"></use></svg></a>
              <button type="button" class="btn btn-xs" data-act="carpeta" title="Mostrar en el Explorador de Windows"><svg class="icon"><use href="#ico-folder-open"></use></svg></button>
            </div>
          </div>`;
        })
        .join("");
      return `
        <div class="dups-group ${g.principal === dups.foco ? "focus" : ""}" data-g="${g.principal}">
          <div class="dups-group-head">
            <span>
              <b class="dups-num">Grupo ${fmtNum(i + 1)} de ${fmtNum(n)}</b>
              · ${g.miembros.length === 2 ? "Estos 2 archivos son idénticos entre sí" : `Estos ${g.miembros.length} archivos son idénticos entre sí`}
              · ${fmtSize(g.size)} cada uno${g.hash ? ` · <span class="dups-hash" title="Huella SHA-256 común a todos los archivos de este grupo">huella ${escapeHtml(g.hash.slice(0, 12))}…</span>` : ""}
            </span>
            <button type="button" class="btn btn-xs btn-danger" data-act="grupo" ${dups.busy ? "disabled" : ""}>
              Eliminar ${sobran === 1 ? "la copia" : `las ${sobran} copias`}
            </button>
          </div>
          ${filas}
        </div>`;
    })
    .join("");
}

function dupsPlan(g) {
  const keep = dups.keep.get(g.principal);
  return { conservar: keep, eliminar: g.miembros.filter((m) => m.id !== keep).map((m) => m.id) };
}

async function resolverDups(grupos) {
  if (dups.busy || !grupos.length) return;
  dups.busy = true;
  renderDups();
  try {
    const r = await apiPost("/api/duplicados/resolver", { grupos });
    const errs = r.errores || [];
    const ok = r.eliminados
      ? `${fmtNum(r.eliminados)} ${r.eliminados === 1 ? "copia enviada" : "copias enviadas"} a la Papelera (${fmtSize(r.bytes)}).`
      : "No se ha eliminado nada.";
    showToast(errs.length ? `${ok} ${errs.length} con problemas: ${errs.slice(0, 3).join(" · ")}` : ok, errs.length ? 10000 : 5000);
  } catch (err) {
    showToast(err.message, 7000);
  } finally {
    dups.busy = false;
  }
  await loadDups();
  loadInfo();
  loadEtiquetas();
  await runSearch(false, { keepPreview: true });
  if (state.selectedDocId) selectDocument(state.selectedDocId, false);
}

function initDupsUI() {
  document.getElementById("btnDuplicados").addEventListener("click", () => openDupsModal());
  document.getElementById("btnCloseDups").addEventListener("click", closeDupsModal);
  document.getElementById("btnDoneDups").addEventListener("click", closeDupsModal);
  document.getElementById("prevCopias").addEventListener("click", () => state.selectedDocId && openDupsModal(state.selectedDocId));
  document.getElementById("btnDupsTodos").addEventListener("click", () => {
    const planes = dups.grupos.map(dupsPlan);
    const n = planes.reduce((a, p) => a + p.eliminar.length, 0);
    if (!confirm(`¿Enviar a la Papelera ${fmtNum(n)} copias (${fmtSize(dups.bytes)}) de ${fmtNum(planes.length)} grupos, ` +
                 "conservando en cada grupo el archivo marcado?")) return;
    resolverDups(planes);
  });
  document.getElementById("dupsList").addEventListener("click", async (e) => {
    const grupoEl = e.target.closest(".dups-group");
    if (!grupoEl) return;
    const g = dups.grupos.find((x) => String(x.principal) === grupoEl.dataset.g);
    if (!g) return;
    const act = e.target.closest("[data-act]");
    if (act && act.dataset.act === "grupo") return resolverDups([dupsPlan(g)]);
    const fila = e.target.closest(".dups-member");
    if (!fila) return;
    if (act && act.dataset.act === "carpeta") {
      try {
        await apiPost("/api/open_location", { archivo_id: parseInt(fila.dataset.id, 10) });
      } catch (err) {
        showToast(err.message, 5000);
      }
      return;
    }
    if (e.target.closest("a")) return; // abrir el PDF
    dups.keep.set(g.principal, parseInt(fila.dataset.id, 10));
    renderDups();
  });
}

// ============================================================================
// OCR DE LOS ESCANEADOS (OCR de Windows: en segundo plano y, uno a uno, con el botón)
// ============================================================================

function textoEstado(doc) {
  if (doc.ocr === 1) return "Sí <span class='ocr-done' title='Texto reconocido por OCR: puede contener errores'>reconocido por OCR</span>";
  if (!doc.necesita_ocr) return "Sí";
  return doc.ocr === 2
    ? "<span class='ocr-badge'>Escaneado: el OCR no ha reconocido texto</span>"
    : "<span class='ocr-badge'>Escaneado sin capa de texto (pendiente de OCR)</span>";
}

function updateOcrButton() {
  const btn = document.getElementById("btnOcr");
  const doc = state.selectedDoc;
  const ver = !!doc && esPdf(doc) && !state.remoto && state.ocrDisponible && (doc.necesita_ocr || doc.ocr === 1);
  btn.style.display = ver ? "" : "none";
  if (!ver) return;
  btn.disabled = state.ocrEnCurso;
  btn.querySelector("span").textContent = state.ocrEnCurso ? "Reconociendo…" : doc.ocr ? "Repetir OCR" : "OCR";
  btn.title = doc.ocr === 1
    ? "Volver a reconocer el texto del documento (OCR de Windows)"
    : "Reconocer ahora el texto del documento escaneado (OCR de Windows)";
}

async function ocrDocumento() {
  const doc = state.selectedDoc;
  if (!doc || state.ocrEnCurso) return;
  state.ocrEnCurso = true;
  updateOcrButton();
  showToast(`Reconociendo el texto de «${doc.nombre}»… (unos segundos por página)`, 5000);
  try {
    const data = await apiPost(`/api/doc/${doc.id}/ocr`, {});
    if (data.doc && state.selectedDocId === doc.id) {
      state.selectedDoc = data.doc;
      updatePreviewPane();
    }
    showToast(data.con_texto
      ? `Texto reconocido: ${fmtNum(data.caracteres)} caracteres. Ya se puede buscar por su contenido.`
      : "El OCR no ha reconocido texto (¿manuscrito o solo imagen?).", 5000);
    await loadInfo();
    await runSearch(false, { keepPreview: true });
  } catch (err) {
    showToast(`No se ha podido hacer el OCR: ${err.message}`, 6000);
  } finally {
    state.ocrEnCurso = false;
    updateOcrButton();
  }
}

// Mientras el OCR en segundo plano avanza, se actualizan de vez en cuando el recuento de escaneados
// y los resultados (los documentos reconocidos pierden la marca «OCR»)
function seguirOcr(o) {
  if (!o) return;
  if (o.activo && o.hechos - state.ocrHechos >= 25) {
    state.ocrHechos = o.hechos;
    loadInfo();
  }
  if (state.ocrActivo && !o.activo && !state.renaming) {
    state.ocrHechos = 0;
    loadInfo();
    runSearch(false, { keepPreview: true });
  }
  state.ocrActivo = !!o.activo;
}

// ============================================================================
// CALENDARIO (fecha de modificación del archivo, la única fecha que se muestra)
// ============================================================================

const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const DIAS_SEMANA = ["L", "M", "X", "J", "V", "S", "D"];
const cal = { datos: null, peticion: 0 };

function mesDe(fecha) {
  return fecha.slice(0, 7);
}

function mesActual() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function sumarMeses(mes, n) {
  const [y, m] = mes.split("-").map(Number);
  const d = new Date(y, m - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function nombreMes(mes) {
  const [y, m] = mes.split("-").map(Number);
  return `${MESES[m - 1]} ${y}`;
}

function textoFecha(f) {
  if (f.length === 7) return `todo ${nombreMes(f)}`;
  const [y, m, d] = f.split("-");
  return `${d}/${m}/${y}`;
}

async function loadCalendar() {
  if (!state.calMes) state.calMes = state.fecha ? mesDe(state.fecha) : mesActual();
  const n = ++cal.peticion;
  const p = new URLSearchParams(searchParamsObject());
  p.delete("fecha");
  p.set("mes", state.calMes);
  try {
    const data = await (await fetch(`/api/calendario?${p.toString()}`)).json();
    if (n !== cal.peticion || data.error) return;
    cal.datos = data;
    renderCalendar();
  } catch (e) {
    /* servidor no disponible momentáneamente */
  }
}

function renderCalendar() {
  const datos = cal.datos || { dias: {}, total: 0 };
  const mes = state.calMes;
  const [y, m] = mes.split("-").map(Number);
  const mesBtn = document.getElementById("btnCalMes");
  mesBtn.textContent = `${nombreMes(mes)} · ${fmtNum(datos.total)}`;
  mesBtn.classList.toggle("sel", state.fecha === mes);
  mesBtn.disabled = !datos.total && state.fecha !== mes;

  const max = Math.max(1, ...Object.values(datos.dias));
  const hoy = new Date();
  const esHoy = (d) => hoy.getFullYear() === y && hoy.getMonth() + 1 === m && hoy.getDate() === d;
  const hueco = (new Date(y, m - 1, 1).getDay() + 6) % 7; // lunes = 0
  const nDias = new Date(y, m, 0).getDate();
  let html = DIAS_SEMANA.map((d) => `<span class="cal-dow">${d}</span>`).join("");
  html += "<span></span>".repeat(hueco);
  for (let d = 1; d <= nDias; d++) {
    const n = datos.dias[String(d)] || 0;
    const f = `${mes}-${String(d).padStart(2, "0")}`;
    const nivel = n ? Math.min(4, 1 + Math.floor((3 * n) / max)) : 0;
    const cls = ["cal-dia", n ? `n${nivel}` : "vacio", state.fecha === f ? "sel" : "", esHoy(d) ? "hoy" : ""].join(" ");
    const tit = n ? `${textoFecha(f)}: ${fmtNum(n)} ${n === 1 ? "documento" : "documentos"}` : `${textoFecha(f)}: sin documentos`;
    html += `<button type="button" class="${cls}" data-fecha="${f}" title="${tit}" ${n || state.fecha === f ? "" : "disabled"}>${d}</button>`;
  }
  document.getElementById("calGrid").innerHTML = html;

  const filtro = document.getElementById("calFiltro");
  filtro.style.display = state.fecha ? "" : "none";
  if (state.fecha) document.getElementById("calFiltroText").textContent = `Solo ${textoFecha(state.fecha)}`;
  document.getElementById("calSection").classList.toggle("activo", !!state.fecha);
  document.getElementById("btnCalNext").disabled = !!datos.ultimo && mes >= datos.ultimo;
  document.getElementById("btnCalPrev").disabled = !!datos.primero && mes <= datos.primero;
}

function elegirFecha(f) {
  state.fecha = state.fecha === f ? "" : f; // otro clic en lo mismo lo quita
  runSearch(true);
  if (state.fecha && esMovil()) setVista("resultados");
}

function irAMes(mes) {
  state.calMes = mes;
  loadCalendar();
}

function initCalendarUI() {
  document.getElementById("calGrid").addEventListener("click", (e) => {
    const b = e.target.closest(".cal-dia");
    if (b && !b.disabled) elegirFecha(b.dataset.fecha);
  });
  document.getElementById("btnCalMes").addEventListener("click", () => elegirFecha(state.calMes));
  document.getElementById("btnCalPrev").addEventListener("click", () => irAMes(sumarMeses(state.calMes, -1)));
  document.getElementById("btnCalNext").addEventListener("click", () => irAMes(sumarMeses(state.calMes, 1)));
  document.getElementById("btnCalPrevYear").addEventListener("click", () => irAMes(sumarMeses(state.calMes, -12)));
  document.getElementById("btnCalNextYear").addEventListener("click", () => irAMes(sumarMeses(state.calMes, 12)));
  document.getElementById("btnCalHoy").addEventListener("click", () => irAMes(mesActual()));
  document.getElementById("btnCalQuitar").addEventListener("click", () => {
    state.fecha = "";
    runSearch(true);
  });
}
