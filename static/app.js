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

function searchParamsObject() {
  const o = {};
  SEARCH_KEYS.forEach((k) => state[k].trim() && (o[k] = state[k].trim()));
  if (state.filtroEt.size) o.et = [...state.filtroEt].join(",");
  if (state.filtroEtx.size) o.etx = [...state.filtroEtx].join(",");
  if (state.sinEt) o.sin_et = "1";
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

    document.getElementById("kpiTotalDocs").textContent = fmtNum(state.total_count);
    const fromIdx = state.total_count === 0 ? 0 : state.offset + 1;
    const toIdx = Math.min(state.offset + state.items.length, state.total_count);
    document.getElementById("paginationInfo").textContent = `Mostrando ${fmtNum(fromIdx)}-${fmtNum(toIdx)} de ${fmtNum(state.total_count)} documentos`;
    document.getElementById("btnPrevPage").disabled = state.offset <= 0;
    document.getElementById("btnNextPage").disabled = state.offset + state.limit >= state.total_count;

    // Los marcados que ya no aparecen dejan de estarlo
    const visibles = new Set(state.items.map((it) => it.id));
    [...state.checked].forEach((id) => !visibles.has(id) && state.checked.delete(id));

    renderGridRows();
    updateBulkUI();
    const actual = state.items.find((it) => it.id === state.selectedDocId);
    if (state.items.length > 0 && !actual) {
      // Si el documento sale de los resultados (p. ej. al etiquetarlo con un filtro activo), pasar al siguiente
      const idx = opts.keepPreview && prevIdx >= 0 ? Math.min(prevIdx, state.items.length - 1) : 0;
      selectDocument(state.items[idx].id, !!opts.keepPreview);
    } else if (actual && opts.keepPreview) {
      if (state.selectedDoc) state.selectedDoc.etiquetas = actual.etiquetas || [];
      renderPreviewTags();
    } else if (state.selectedDoc && state.activeTab !== "pdf") {
      updatePreviewPane();
    }
  } catch (err) {
    console.error("Error en búsqueda:", err);
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">Error en la búsqueda: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderGridRows() {
  const tbody = document.getElementById("resultsTbody");
  if (!state.items.length) {
    tbody.innerHTML = `
      <tr><td colspan="4" class="empty-cell">
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
      const chk = state.checked.has(it.id);
      return `
        <tr data-id="${it.id}" class="${sel} ${chk ? "checked" : ""}">
          <td class="cell-check"><input type="checkbox" class="row-check" ${chk ? "checked" : ""} /></td>
          <td>
            <div class="date-cell">${formatDateFromEpoch(it.mtime)}</div>
            <span class="date-sub">${pags}</span>${ocr}
          </td>
          <td>
            <div class="doc-name">${highlightText(it.nombre, terms)}</div>
            ${tagPillsHtml(it.etiquetas)}
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
      if (e.ctrlKey || e.metaKey) return toggleCheck(idx, !state.checked.has(id), false);
      if (e.shiftKey && state.lastCheckIdx !== null) return toggleCheck(idx, true, true);
      selectDocument(id, false);
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
    document.getElementById("prevSubtitle").textContent = [
      `Modificado ${formatDateFromEpoch(doc.mtime)}`,
      `${doc.num_paginas} pág.`,
      doc.ruta_rel,
    ].join("  ·  ");
    renderPreviewTags();
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
    state.filtroEt.clear();
    state.filtroEtx.clear();
    state.sinEt = false;
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

  // Teclado: ↑ / ↓ recorre, 1-9 pone/quita etiquetas, Esc cierra el menú o desmarca
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (tagPopoverOpen()) return closeTagPopover();
      if (document.getElementById("tagsModal").style.display !== "none") return closeTagsModal();
      if (state.checked.size && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return clearChecks();
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && document.activeElement.type !== "checkbox") return;
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

  initTagsUI();
  await loadInfo();
  await loadEtiquetas();
  await runSearch(true);
  pollStatus();
  document.getElementById("qTodas").focus();
});

// ============================================================================
// ETIQUETAS (manuales: el programa nunca etiqueta por su cuenta)
// ============================================================================

async function apiPost(url, payload) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) });
  const data = await res.json().catch(() => ({}));
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

function tagPill(t, extra = "") {
  return `<span class="tag-pill" style="--tag:${t.color}" data-tag-id="${t.id}">${escapeHtml(t.nombre)}${extra}</span>`;
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
    sortedTagIds(doc.etiquetas).map((id) => tagPill(state.etMap[id], `<button type="button" class="tag-x" title="Quitar etiqueta">&times;</button>`)).join("") +
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
