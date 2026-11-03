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
};

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

function fmtSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function showToast(msg, duration = 3800) {
  const c = document.getElementById("toastContainer");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

function hasSearch() {
  return SEARCH_KEYS.some((k) => state[k].trim());
}

function buildQueryParams() {
  const p = new URLSearchParams();
  SEARCH_KEYS.forEach((k) => state[k].trim() && p.set(k, state[k].trim()));
  p.set("sort_by", state.sort_by);
  p.set("limit", String(state.limit));
  p.set("offset", String(state.offset));
  return p;
}

function termsQuery() {
  return state.terms.map((t) => `&t=${encodeURIComponent(t)}`).join("");
}

// ============================================================================
// INFORMACIÓN GENERAL Y BÚSQUEDA
// ============================================================================

async function loadInfo() {
  try {
    const data = await (await fetch("/api/info")).json();
    const st = data.stats || {};
    document.getElementById("folderStatusText").textContent =
      `${data.folder_display} — ${fmtNum(st.total_docs)} documentos (${fmtNum(st.total_pages)} págs.) · ` +
      `${fmtNum(st.sin_texto)} escaneados sin texto`;
    if (data.ui_layout && !window._layoutInitializedFromDisk) {
      window._layoutInitializedFromDisk = true;
      applyPanelLayout(data.ui_layout, false);
    }
    updateIndexProgress(data.estado);
  } catch (err) {
    console.error("Error cargando información:", err);
  }
}

async function runSearch(resetOffset = true) {
  if (resetOffset) state.offset = 0;
  const tbody = document.getElementById("resultsTbody");
  try {
    const res = await fetch(`/api/search?${buildQueryParams().toString()}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.total_count = data.total_count || 0;
    state.items = data.items || [];
    state.terms = data.terms || [];

    document.getElementById("kpiTotalDocs").textContent = fmtNum(state.total_count);
    const fromIdx = state.total_count === 0 ? 0 : state.offset + 1;
    const toIdx = Math.min(state.offset + state.items.length, state.total_count);
    document.getElementById("paginationInfo").textContent = `Mostrando ${fmtNum(fromIdx)}-${fmtNum(toIdx)} de ${fmtNum(state.total_count)} documentos`;
    document.getElementById("btnPrevPage").disabled = state.offset <= 0;
    document.getElementById("btnNextPage").disabled = state.offset + state.limit >= state.total_count;

    renderGridRows();
    if (state.items.length > 0 && !state.items.find((it) => it.id === state.selectedDocId)) {
      selectDocument(state.items[0].id, false);
    } else if (state.selectedDoc && state.activeTab !== "pdf") {
      updatePreviewPane();
    }
  } catch (err) {
    console.error("Error en búsqueda:", err);
    tbody.innerHTML = `<tr><td colspan="3" class="empty-cell">Error en la búsqueda: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderGridRows() {
  const tbody = document.getElementById("resultsTbody");
  if (!state.items.length) {
    tbody.innerHTML = `
      <tr><td colspan="3" class="empty-cell">
        No hay documentos que cumplan la búsqueda.<br/>
        <small>Prueba con menos palabras, con el comienzo de una palabra o revisa el campo «No contiene».</small>
      </td></tr>`;
    return;
  }
  const terms = state.terms;
  tbody.innerHTML = state.items
    .map((it) => {
      const sel = it.id === state.selectedDocId ? "selected" : "";
      const pags = it.num_paginas > 1 ? `${it.num_paginas} págs.` : "1 pág.";
      const ocr = it.sin_texto ? `<span class="ocr-badge" title="Escaneado sin capa de texto: solo se encuentra por nombre y carpeta">OCR</span>` : "";
      const snippet = it.snippet ? `<div class="snippet-match">${highlightText(it.snippet, terms)}</div>` : "";
      const dir = it.ruta_rel.replace(/[\\/][^\\/]*$/, "");
      return `
        <tr data-id="${it.id}" class="${sel}">
          <td>
            <div class="date-cell">${formatDateFromEpoch(it.mtime)}</div>
            <span class="date-sub">${pags}</span>${ocr}
          </td>
          <td>
            <div class="doc-name">${highlightText(it.nombre, terms)}</div>
            ${hasSearch() ? snippet : ""}
          </td>
          <td>
            <div class="folder-path" title="${escapeHtml(it.ruta_rel)}">${highlightText(dir === it.ruta_rel ? "(raíz)" : dir, terms)}</div>
          </td>
        </tr>`;
    })
    .join("");
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => selectDocument(parseInt(tr.dataset.id, 10), false));
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
    document.getElementById("prevSubtitle").textContent = [
      `Modificado ${formatDateFromEpoch(doc.mtime)}`,
      `${doc.num_paginas} pág.`,
      doc.ruta_rel,
    ].join("  ·  ");
    document.getElementById("previewToolbar").style.display = "flex";
    document.getElementById("previewPlaceholder").style.display = "none";
    document.getElementById("btnDownloadSinglePdf").href = `/api/doc/${doc.id}/pdf?download=1`;
    document.getElementById("btnOpenNewTab").href = `/api/doc/${doc.id}/pdf`;
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

  if (state.activeTab === "highlight") {
    paneHighlight.style.display = "block";
    bar.style.display = "flex";
    document.getElementById("docPageIndicator").textContent = `Página ${state.currentDocPage} de ${doc.num_paginas}`;
    document.getElementById("btnPrevDocPage").disabled = state.currentDocPage <= 1;
    document.getElementById("btnNextDocPage").disabled = state.currentDocPage >= doc.num_paginas;
    document.getElementById("highlightImg").src = `/api/doc/${doc.id}/page_image?page=${state.currentDocPage}${termsQuery()}`;
    applyZoom();
  } else if (state.activeTab === "pdf") {
    panePdf.style.display = "block";
    bar.style.display = "none";
    const iframe = document.getElementById("pdfIframe");
    const target = `/api/doc/${doc.id}/pdf`;
    if (!iframe.src.endsWith(target)) iframe.src = target;
  } else {
    paneText.style.display = "block";
    bar.style.display = "none";
    document.getElementById("docMetadataGrid").innerHTML = `
      <div class="meta-item"><strong>Archivo</strong>${escapeHtml(doc.nombre)}</div>
      <div class="meta-item"><strong>Modificado</strong>${formatDateFromEpoch(doc.mtime)}</div>
      <div class="meta-item"><strong>Páginas / tamaño</strong>${doc.num_paginas} · ${fmtSize(doc.size)}</div>
      <div class="meta-item"><strong>Texto</strong>${doc.necesita_ocr ? "<span class='ocr-badge'>Escaneado sin capa de texto</span>" : "Sí"}</div>
      <div class="meta-item" style="grid-column: 1 / -1;"><strong>Ruta</strong>${escapeHtml(doc.ruta_display)}</div>`;
    const txt = doc.texto || (doc.necesita_ocr ? "(Documento escaneado sin capa de texto: pendiente de OCR)" : "");
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
    updateIndexProgress(estado);
    if (state.wasIndexing && !estado.activo) {
      const u = estado.ultimo || {};
      if (u.status === "ok" && (u.added || u.updated || u.removed)) {
        showToast(`Biblioteca actualizada: +${fmtNum(u.added)} nuevos, ${fmtNum(u.updated)} modificados, ${fmtNum(u.removed)} eliminados.`);
        await loadInfo();
        await runSearch(false);
      }
    }
    state.wasIndexing = !!estado.activo;
  } catch (e) {
    /* servidor no disponible momentáneamente */
  }
}

async function triggerSync(payload = {}) {
  const data = await (
    await fetch("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
  ).json();
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

async function openPrefsModal() {
  try {
    const data = await (await fetch("/api/config")).json();
    const cfg = data.config || {};
    document.getElementById("prefBiblioteca").value = cfg.biblioteca || "";
    document.getElementById("prefIncludeSubfolders").checked = cfg.include_subfolders !== false;
    document.getElementById("prefInterval").value = cfg.sync_interval_min || 10;
    const list = document.getElementById("excludedListContainer");
    list.innerHTML = "";
    (cfg.excluded_dirs || []).forEach((d) => appendCatalogRow("excludedListContainer", d));
    document.getElementById("prefFolderStatus").textContent = data.folder.exists
      ? `Carpeta verificada: ${data.folder.display}`
      : "Atención: la carpeta indicada no existe o no es accesible.";
    document.getElementById("prefsModal").style.display = "flex";
  } catch (err) {
    showToast("Error al abrir las preferencias: " + err.message);
  }
}

async function savePrefs() {
  const payload = {
    biblioteca: document.getElementById("prefBiblioteca").value.trim(),
    include_subfolders: document.getElementById("prefIncludeSubfolders").checked,
    sync_interval_min: parseInt(document.getElementById("prefInterval").value, 10) || 10,
    excluded_dirs: readCatalogList("excludedListContainer", "newExcludedInput"),
  };
  try {
    await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    document.getElementById("prefsModal").style.display = "none";
    showToast("Preferencias guardadas. Sincronizando en segundo plano…");
    state.wasIndexing = true;
  } catch (err) {
    showToast("Error guardando preferencias: " + err.message);
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
  if (persist) {
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
    btn.addEventListener("click", () => {
      document.querySelectorAll(".view-tabs .tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_id: state.selectedDocId }),
    });
    if (!r.ok) showToast("No se ha podido abrir la carpeta del documento.");
  });

  // Teclado ↑ / ↓
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
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
  document.getElementById("btnOpenPrefs").addEventListener("click", openPrefsModal);
  document.getElementById("btnClosePrefs").addEventListener("click", closePrefs);
  document.getElementById("btnCancelPrefs").addEventListener("click", closePrefs);
  document.getElementById("btnSavePrefs").addEventListener("click", savePrefs);
  document.getElementById("btnReindexAll").addEventListener("click", () => {
    if (!confirm("¿Volver a leer todos los documentos desde cero? Tardará unos minutos.")) return;
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

  await loadInfo();
  await runSearch(true);
  pollStatus();
  document.getElementById("qTodas").focus();
});
