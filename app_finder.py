#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FinderJR - Servidor web local de búsqueda de texto completo en la Biblioteca
-----------------------------------------------------------------------------
Interfaz replicada de cBank (Búsqueda | Resultados | Previsualización). Búsqueda bruta
sobre el texto, el nombre y la ruta de cada documento: todas las palabras, alguna,
frase exacta y exclusión. Indexación incremental en segundo plano.
Sin frameworks: ThreadingHTTPServer + PyMuPDF + SQLite FTS5.
"""

import csv
import datetime
import io
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import fitz  # PyMuPDF

from finder import etiquetas, indexador
from finder.config import BASE_DIR, load_config, save_config, to_display_path
from finder.db import get_conn, init_db, meta_get
from finder.extraccion import SEPARADOR_PAGINA

STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_PORT = 8766

HEARTBEAT_LOCK = threading.Lock()
ACTIVE_CLIENTS: Dict[str, float] = {}
CLIENT_EVER_CONNECTED = False
LAST_CLIENT_LEFT_AT: Optional[float] = None
LAST_HTTP_ACTIVITY_AT = time.monotonic()
ACTIVE_HTTP_REQUESTS = 0
SERVER_READY_AT = time.monotonic()

# Pesos BM25 por columna: nombre de archivo, ruta, texto
BM25 = "bm25(documentos_fts, 8.0, 4.0, 1.0)"


# ==============================================================================
# BÚSQUEDA
# ==============================================================================

def expand_numeric_variants(token: str) -> List[str]:
    """'1224,00' <-> '1.224,00' para encontrar cifras con o sin punto de miles."""
    variants = [token]
    m = re.match(r"^(\d{4,})(,\d{1,2})?$", token)
    if m:
        dotted = f"{int(m.group(1)):,}".replace(",", ".") + (m.group(2) or "")
        if dotted not in variants:
            variants.append(dotted)
    elif re.match(r"^\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?$", token):
        variants.append(token.replace(".", ""))
    return variants


def _subtokens(s: str) -> List[str]:
    return re.findall(r"[0-9A-Za-zÀ-ÿ]+", s)


def _word_group(tok: str) -> Optional[str]:
    """Expresión FTS5 de una palabra: prefijo, variantes numéricas y separadores ('B-06976' = 'B06976')."""
    parts = []
    for v in expand_numeric_variants(tok):
        sub = _subtokens(v)
        if not sub:
            continue
        if len(sub) == 1:
            parts.append(f'"{sub[0]}"*')
        else:
            parts.append('"' + " ".join(sub) + '"*')
            parts.append(f'"{"".join(sub)}"*')
    if not parts:
        return None
    parts = list(dict.fromkeys(parts))
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _phrases(raw: str) -> List[str]:
    """Una frase por campo; si el usuario escribe varias entre comillas, se respetan todas."""
    raw = (raw or "").strip()
    if not raw:
        return []
    quoted = re.findall(r'"([^"]+)"', raw)
    return [q.strip() for q in quoted if q.strip()] if quoted else [raw]


def parse_search(params: Dict[str, str]) -> Dict[str, Any]:
    todas = (params.get("todas") or "").split()[:10]
    alguna = (params.get("alguna") or "").split()[:10]
    frases = _phrases(params.get("frase") or "")[:4]
    excluye = (params.get("excluye") or "").split()[:10]

    pos: List[str] = []
    for t in todas:
        g = _word_group(t)
        if g:
            pos.append(g)
    alg = [g for g in (_word_group(t) for t in alguna) if g]
    if alg:
        pos.append(alg[0] if len(alg) == 1 else "(" + " OR ".join(alg) + ")")
    for f in frases:
        sub = _subtokens(f)
        if sub:
            pos.append('"' + " ".join(sub) + '"')
    exc = [g for g in (_word_group(t) for t in excluye) if g]

    # Términos a resaltar (en el grid, en el texto y sobre la página)
    terms: List[str] = []
    for t in todas + alguna:
        for v in expand_numeric_variants(t.strip('"')):
            if len(v) >= 2 and v not in terms:
                terms.append(v)
    for f in frases:
        if f not in terms:
            terms.append(f)
    return {
        "match": " AND ".join(pos) if pos else None,
        "exclude": " OR ".join(exc) if exc else None,
        "terms": terms,
    }


def build_where(ps: Dict[str, Any], params: Dict[str, str]) -> Tuple[str, List[Any]]:
    clauses, sp = ["1=1"], []
    if ps["match"]:
        clauses.append("d.id IN (SELECT rowid FROM documentos_fts WHERE documentos_fts MATCH ?)")
        sp.append(ps["match"])
    if ps["exclude"]:
        clauses.append("d.id NOT IN (SELECT rowid FROM documentos_fts WHERE documentos_fts MATCH ?)")
        sp.append(ps["exclude"])
    et_clauses, et_sp = etiquetas.filtros_sql(params)
    return " AND ".join(clauses + et_clauses), sp + et_sp


def run_query(params: Dict[str, str], limit: int, offset: int, cols: str) -> Tuple[int, List[Any], Dict[str, Any]]:
    ps = parse_search(params)
    sort_by = params.get("sort_by") or "relevancia"
    conn = get_conn()
    try:
        if ps["match"] and sort_by == "relevancia":
            # Relevancia BM25: se consulta el índice FTS directamente para obtener la puntuación
            sql_from = "FROM documentos_fts f JOIN documentos d ON d.id = f.rowid WHERE documentos_fts MATCH ?"
            sp: List[Any] = [ps["match"]]
            if ps["exclude"]:
                sql_from += " AND d.id NOT IN (SELECT rowid FROM documentos_fts WHERE documentos_fts MATCH ?)"
                sp.append(ps["exclude"])
            et_clauses, et_sp = etiquetas.filtros_sql(params)
            for c in et_clauses:
                sql_from += " AND " + c
            sp += et_sp
            total = conn.execute(f"SELECT COUNT(*) {sql_from}", sp).fetchone()[0]
            rows = conn.execute(f"SELECT {cols} {sql_from} ORDER BY {BM25} LIMIT ? OFFSET ?", sp + [limit, offset]).fetchall()
        else:
            order = {
                "fecha_asc": "d.mtime ASC, d.id ASC",
                "file_asc": "d.nombre ASC",
                "file_desc": "d.nombre DESC",
            }.get(sort_by, "d.mtime DESC, d.id DESC")
            where, sp = build_where(ps, params)
            total = conn.execute(f"SELECT COUNT(*) FROM documentos d WHERE {where}", sp).fetchone()[0]
            rows = conn.execute(f"SELECT {cols} FROM documentos d WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                                sp + [limit, offset]).fetchall()
    finally:
        conn.close()
    return total, rows, ps


def _fold(s: str) -> str:
    """Minúsculas sin acentos conservando la longitud (para localizar términos en el texto)."""
    return "".join(unicodedata.normalize("NFD", c)[0] for c in s).lower()


def make_snippet(text: str, terms: List[str]) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text.replace(SEPARADOR_PAGINA.strip(), " | ")).strip()
    folded = _fold(clean)
    pos = -1
    for t in terms:
        p = folded.find(_fold(t))
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return clean[:180] + ("…" if len(clean) > 180 else "")
    s, e = max(0, pos - 60), min(len(clean), pos + 160)
    return ("…" if s else "") + clean[s:e] + ("…" if e < len(clean) else "")


def query_documents(params: Dict[str, str]) -> Dict[str, Any]:
    try:
        limit = min(max(int(params.get("limit", "200")), 10), 1000)
    except ValueError:
        limit = 200
    try:
        offset = max(int(params.get("offset", "0")), 0)
    except ValueError:
        offset = 0
    cols = "d.id, d.nombre, d.ruta_rel, d.carpeta, d.mtime, d.num_paginas, d.sin_texto, d.texto"
    total, rows, ps = run_query(params, limit, offset, cols)
    items = []
    for r in rows:
        it = dict(r)
        it["snippet"] = make_snippet(it.pop("texto") or "", ps["terms"])
        items.append(it)
    if items:
        conn = get_conn()
        try:
            por_ruta = etiquetas.de_rutas(conn, [it["ruta_rel"] for it in items])
        finally:
            conn.close()
        for it in items:
            it["etiquetas"] = por_ruta.get(it["ruta_rel"], [])
    return {"total_count": total, "limit": limit, "offset": offset, "terms": ps["terms"], "items": items}


def query_info() -> Dict[str, Any]:
    conn = get_conn()
    stats = dict(conn.execute(
        """SELECT (SELECT COUNT(*) FROM documentos) AS total_docs,
                  (SELECT COALESCE(SUM(paginas), 0) FROM archivos) AS total_pages,
                  (SELECT COUNT(*) FROM archivos WHERE necesita_ocr = 1) AS sin_texto""").fetchone())
    ultima = meta_get(conn, "ultima_sync")
    conn.close()
    fi = indexador.folder_info()
    return {"stats": stats, "ultima_sync": ultima, "folder_display": fi["display"], "folder_exists": fi["exists"],
            "ui_layout": load_config().get("ui_layout", {}), "estado": indexador.get_state()}


def get_doc(doc_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    r = conn.execute(
        """SELECT d.*, a.ruta, a.size, a.paginas, a.necesita_ocr, a.estado, a.error
           FROM documentos d JOIN archivos a ON a.id = d.archivo_id WHERE d.id = ?""", (doc_id,)).fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    d["ruta_display"] = to_display_path(d["ruta"])
    conn = get_conn()
    try:
        d["etiquetas"] = etiquetas.de_rutas(conn, [d["ruta_rel"]])[d["ruta_rel"]]
    finally:
        conn.close()
    return d


def render_page_png(doc: Dict[str, Any], rel_page: int, terms: List[str]) -> bytes:
    with fitz.open(doc["ruta"]) as pdf:
        idx = max(0, min(len(pdf) - 1, int(doc["primera_pag"]) - 1 + rel_page - 1))
        page = pdf[idx]
        for term in terms[:12]:
            try:
                for rect in page.search_for(term):
                    a = page.add_highlight_annot(rect)
                    a.set_colors(stroke=(1.0, 0.92, 0.15))
                    a.update()
            except Exception:  # noqa: BLE001
                pass
        return page.get_pixmap(dpi=150, alpha=False).tobytes("png")


def csv_export(params: Dict[str, str]) -> bytes:
    _, rows, _ = run_query(params, 100000, 0, "d.id, d.mtime, d.nombre, d.ruta_rel, d.carpeta, d.num_paginas, d.sin_texto")
    conn = get_conn()
    try:
        nombres = {e["id"]: e["nombre"] for e in etiquetas.listar(conn)["items"]}
        por_ruta = etiquetas.de_rutas(conn, [r["ruta_rel"] for r in rows])
    finally:
        conn.close()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["ID", "Fecha_modificacion", "Archivo", "Ruta", "Carpeta", "Paginas", "Sin_texto", "Etiquetas"])
    for r in rows:
        fecha = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%d/%m/%Y") if r["mtime"] else ""
        ets = ", ".join(sorted((nombres[i] for i in por_ruta.get(r["ruta_rel"], []) if i in nombres), key=str.lower))
        w.writerow([r["id"], fecha, r["nombre"], r["ruta_rel"], r["carpeta"], r["num_paginas"],
                    "SÍ" if r["sin_texto"] else "", ets])
    return out.getvalue().encode("utf-8-sig")


def pdf_bundle(params: Dict[str, str], max_docs: int = 300) -> bytes:
    _, rows, _ = run_query(params, max_docs, 0, "d.archivo_id, d.primera_pag, d.ultima_pag")
    conn = get_conn()
    rutas = {r["id"]: r["ruta"] for r in conn.execute(
        f"SELECT id, ruta FROM archivos WHERE id IN ({','.join('?' * len(rows)) or 'NULL'})", [r["archivo_id"] for r in rows])}
    conn.close()
    out = fitz.open()
    try:
        for r in rows:
            ruta = rutas.get(r["archivo_id"])
            if not ruta or not os.path.exists(ruta) or not ruta.lower().endswith(".pdf"):
                continue
            try:
                with fitz.open(ruta) as src:
                    out.insert_pdf(src, from_page=max(0, r["primera_pag"] - 1), to_page=min(len(src) - 1, r["ultima_pag"] - 1))
            except Exception:  # noqa: BLE001
                continue
        if len(out) == 0:
            out.new_page().insert_text((72, 100), "No se encontraron documentos con la búsqueda actual.")
        return out.tobytes(garbage=1, deflate=True)
    finally:
        out.close()


def etiquetas_listar() -> Dict[str, Any]:
    conn = get_conn()
    try:
        return etiquetas.listar(conn)
    finally:
        conn.close()


def etiquetar(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pone o quita una etiqueta a una lista de documentos o a todos los resultados de una búsqueda."""
    etiqueta_id = int(payload.get("etiqueta_id") or 0)
    poner = payload.get("accion", "poner") != "quitar"
    if isinstance(payload.get("busqueda"), dict):
        params = {k: str(v) for k, v in payload["busqueda"].items()}
        _, rows, _ = run_query(params, 100000, 0, "d.ruta_rel")
        rutas = [r["ruta_rel"] for r in rows]
    else:
        ids = [int(x) for x in payload.get("doc_ids") or [] if str(x).isdigit()]
        conn = get_conn()
        try:
            rutas = [r[0] for r in conn.execute(
                f"SELECT ruta_rel FROM documentos WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)]
        finally:
            conn.close()
    conn = get_conn()
    try:
        cambiados = etiquetas.asignar(conn, etiqueta_id, rutas, poner)
    finally:
        conn.close()
    return {"status": "ok", "documentos": len(rutas), "cambiados": cambiados}


def open_location(path: str) -> None:
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["explorer.exe", "/select,", to_display_path(path)])


def content_disposition(filename: str, inline: bool) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename) or "documento.pdf"
    return (f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{urllib.parse.quote(filename)}")


# ==============================================================================
# SERVIDOR HTTP
# ==============================================================================

class FinderHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.path.startswith(("/api/doc/", "/api/heartbeat", "/api/status", "/static/")):
            try:
                sys.stderr.write(f"[FinderJR] {self.command} {self.path}\n")
            except Exception:  # noqa: BLE001
                pass

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, ctype: str, filename: Optional[str] = None, inline: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", content_disposition(filename, inline))
        self.end_headers()
        self.wfile.write(body)

    def _begin(self) -> None:
        global ACTIVE_HTTP_REQUESTS, LAST_HTTP_ACTIVITY_AT
        with HEARTBEAT_LOCK:
            ACTIVE_HTTP_REQUESTS += 1
            LAST_HTTP_ACTIVITY_AT = time.monotonic()

    def _end(self) -> None:
        global ACTIVE_HTTP_REQUESTS, LAST_HTTP_ACTIVITY_AT
        with HEARTBEAT_LOCK:
            ACTIVE_HTTP_REQUESTS = max(0, ACTIVE_HTTP_REQUESTS - 1)
            LAST_HTTP_ACTIVITY_AT = time.monotonic()

    def do_GET(self) -> None:
        self._begin()
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params = {k: v[0] for k, v in qs.items()}
        try:
            if route == "/api/info":
                return self.send_json(query_info())
            if route == "/api/search":
                return self.send_json(query_documents(params))
            if route == "/api/status":
                return self.send_json(indexador.get_state())
            if route == "/api/config":
                return self.send_json({"config": load_config(), "folder": indexador.folder_info()})
            if route == "/api/etiquetas":
                return self.send_json(etiquetas_listar())

            m = re.match(r"^/api/doc/(\d+)(/pdf|/page_image)?$", route)
            if m:
                doc = get_doc(int(m.group(1)))
                if not doc:
                    return self.send_json({"error": "Documento no encontrado"}, status=404)
                if not m.group(2):
                    return self.send_json(doc)
                if not os.path.exists(doc["ruta"]):
                    return self.send_json({"error": f"No se encuentra el archivo: {doc['ruta_display']}"}, status=404)
                if m.group(2) == "/pdf":
                    with open(doc["ruta"], "rb") as f:
                        data = f.read()
                    ctype = mimetypes.guess_type(doc["nombre"])[0] or "application/octet-stream"
                    return self.send_bytes(data, ctype, filename=doc["nombre"], inline=params.get("download") != "1")
                png = render_page_png(doc, max(1, int(params.get("page", "1"))), [t for t in qs.get("t", []) if t.strip()])
                return self.send_bytes(png, "image/png")

            if route == "/api/export/csv":
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                return self.send_bytes(csv_export(params), "text/csv; charset=utf-8", f"FinderJR_{ts}.csv", inline=False)
            if route == "/api/export/pdf_bundle":
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                return self.send_bytes(pdf_bundle(params), "application/pdf", f"FinderJR_seleccion_{ts}.pdf", inline=False)

            if route in ("/", "/index.html"):
                target = os.path.join(STATIC_DIR, "index.html")
            elif route.startswith("/static/"):
                target = os.path.abspath(os.path.join(STATIC_DIR, route[len("/static/"):]))
                if not target.startswith(os.path.abspath(STATIC_DIR)):
                    return self.send_json({"error": "Acceso denegado"}, status=403)
            else:
                return self.send_json({"error": "Ruta no encontrada"}, status=404)
            if not os.path.isfile(target):
                return self.send_json({"error": "Recurso no encontrado"}, status=404)
            with open(target, "rb") as f:
                content = f.read()
            ctype = mimetypes.guess_type(target)[0] or "text/html"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_bytes(content, ctype)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self.send_json({"error": str(e)}, status=500)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._end()

    def do_POST(self) -> None:
        global CLIENT_EVER_CONNECTED, LAST_CLIENT_LEFT_AT
        self._begin()
        route = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            if route == "/api/heartbeat":
                cid = str(payload.get("client_id") or "default")
                now = time.monotonic()
                with HEARTBEAT_LOCK:
                    if payload.get("action") == "bye":
                        ACTIVE_CLIENTS.pop(cid, None)
                        if not ACTIVE_CLIENTS:
                            LAST_CLIENT_LEFT_AT = now
                    else:
                        CLIENT_EVER_CONNECTED = True
                        ACTIVE_CLIENTS[cid] = now
                        LAST_CLIENT_LEFT_AT = None
                return self.send_json({"status": "ok"})

            if route == "/api/sync":
                started = indexador.start_sync_async(force_reindex=bool(payload.get("force_reindex")))
                return self.send_json({"started": started, "estado": indexador.get_state()})

            if route == "/api/open_location":
                doc = get_doc(int(payload.get("doc_id", 0)))
                if not doc:
                    return self.send_json({"error": "Documento no encontrado"}, status=404)
                open_location(doc["ruta"])
                return self.send_json({"status": "ok"})

            if route == "/api/etiquetas":
                conn = get_conn()
                try:
                    et = etiquetas.crear(conn, str(payload.get("nombre", "")), payload.get("color"))
                finally:
                    conn.close()
                return self.send_json(et)

            m = re.match(r"^/api/etiquetas/(\d+)(/borrar)?$", route)
            if m:
                conn = get_conn()
                try:
                    if m.group(2):
                        etiquetas.borrar(conn, int(m.group(1)))
                    else:
                        etiquetas.modificar(conn, int(m.group(1)), payload)
                finally:
                    conn.close()
                return self.send_json({"status": "ok"})

            if route == "/api/etiquetar":
                return self.send_json(etiquetar(payload))

            if route == "/api/layout":
                cfg = load_config()
                lay = cfg.get("ui_layout", {})
                bounds = {"filters_px": (200, 560), "grid_fr": (0.25, 1.95), "preview_fr": (0.35, 1.95),
                          "col_fecha_px": (60, 220), "col_origen_px": (80, 400)}
                for k, (lo, hi) in bounds.items():
                    if k in payload:
                        try:
                            v = max(lo, min(hi, float(payload[k])))
                            lay[k] = round(v, 3) if k.endswith("_fr") else int(round(v))
                        except Exception:  # noqa: BLE001
                            pass
                lay.pop("col_entidad_px", None)
                cfg["ui_layout"] = lay
                save_config(cfg)
                return self.send_json({"status": "ok", "ui_layout": lay})

            if route == "/api/config":
                cfg = load_config()
                if str(payload.get("biblioteca", "")).strip():
                    cfg["biblioteca"] = str(payload["biblioteca"]).strip()
                if "include_subfolders" in payload:
                    cfg["include_subfolders"] = bool(payload["include_subfolders"])
                if isinstance(payload.get("excluded_dirs"), list):
                    cfg["excluded_dirs"] = [str(x).strip() for x in payload["excluded_dirs"] if str(x).strip()]
                if "sync_interval_min" in payload:
                    try:
                        cfg["sync_interval_min"] = max(1, min(240, int(payload["sync_interval_min"])))
                    except Exception:  # noqa: BLE001
                        pass
                save_config(cfg)
                started = indexador.start_sync_async()
                return self.send_json({"status": "ok", "started": started})

            self.send_json({"error": "Endpoint POST no válido"}, status=404)
        except ValueError as e:
            self.send_json({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(e)}, status=500)
        finally:
            self._end()


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def lifecycle_watchdog(server: FinderHTTPServer) -> None:
    """Apaga el servidor al cerrar la última pestaña, salvo que haya una indexación en curso."""
    global LAST_CLIENT_LEFT_AT
    while True:
        time.sleep(1.0)
        shutdown = False
        with HEARTBEAT_LOCK:
            now = time.monotonic()
            stale = [c for c, ts in ACTIVE_CLIENTS.items() if now - ts > 15.0]
            for c in stale:
                ACTIVE_CLIENTS.pop(c, None)
            if stale and not ACTIVE_CLIENTS and LAST_CLIENT_LEFT_AT is None:
                LAST_CLIENT_LEFT_AT = now
            busy = indexador.get_state().get("activo")
            if ACTIVE_HTTP_REQUESTS == 0 and not busy:
                if CLIENT_EVER_CONNECTED and not ACTIVE_CLIENTS:
                    if now - (LAST_CLIENT_LEFT_AT or now) >= 4.0 and now - LAST_HTTP_ACTIVITY_AT >= 3.5:
                        shutdown = True
                elif not CLIENT_EVER_CONNECTED and now - SERVER_READY_AT >= 90.0:
                    shutdown = True
        if shutdown:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            break


def main() -> None:
    global SERVER_READY_AT
    port = int(os.environ.get("FINDERJR_PORT", DEFAULT_PORT))
    url = f"http://localhost:{port}"
    if is_port_in_use(port):
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    init_db()
    server = FinderHTTPServer(("127.0.0.1", port), Handler)
    SERVER_READY_AT = time.monotonic()

    stop = threading.Event()
    threading.Thread(target=indexador.background_loop, args=(stop,), daemon=True, name="finder-bg").start()
    if "--no-auto-shutdown" not in sys.argv:
        threading.Thread(target=lifecycle_watchdog, args=(server,), daemon=True).start()
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
