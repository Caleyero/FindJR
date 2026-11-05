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
import html
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

from finder import config, duplicados, etiquetas, indexador, remoto
from finder.config import BASE_DIR, fuente, load_config, save_config, to_display_path
from finder.db import borrar_indice, get_conn, init_db, meta_get, meta_set
from finder.extraccion import SEPARADOR_PAGINA, es_pdf

STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_PORT = 8766

HEARTBEAT_LOCK = threading.Lock()
ACTIVE_CLIENTS: Dict[str, float] = {}
CLIENT_EVER_CONNECTED = False
LAST_CLIENT_LEFT_AT: Optional[float] = None
LAST_HTTP_ACTIVITY_AT = time.monotonic()
ACTIVE_HTTP_REQUESTS = 0
SERVER_READY_AT = time.monotonic()

# Pesos BM25 por columna: nombre de archivo, ruta, texto, rutas de sus copias idénticas
BM25 = "bm25(documentos_fts, 8.0, 4.0, 1.0, 4.0)"


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
    cols = ("d.id, d.nombre, d.ruta_rel, d.carpeta, d.mtime, d.num_paginas, d.sin_texto, d.texto, "
            "(SELECT COUNT(*) FROM archivos c WHERE c.copia_de = d.archivo_id) AS copias, d.otras_rutas")
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
                  (SELECT COALESCE(SUM(paginas), 0) FROM archivos WHERE copia_de IS NULL) AS total_pages,
                  (SELECT COUNT(*) FROM archivos WHERE necesita_ocr = 1 AND copia_de IS NULL) AS sin_texto""").fetchone())
    dup = duplicados.contar(conn)
    stats.update(duplicados=dup["copias"], duplicados_bytes=dup["bytes"])
    ultima = meta_get(conn, "ultima_sync")
    conn.close()
    cfg = load_config()
    fte = fuente(cfg)
    fi = indexador.folder_info(fte["id"])
    return {"stats": stats, "ultima_sync": ultima, "folder_display": fi["display"], "folder_exists": fi["exists"],
            "folder_configurada": fi["configurada"], "ui_layout": cfg.get("ui_layout", {}),
            "estado": indexador.get_state(), "fuente": _fuente_publica(fte), "fuentes": fuentes_listar()["items"]}


# ------------------------------------------------------------------------------
# Fuentes de documentos (cada una con su índice y sus etiquetas)
# ------------------------------------------------------------------------------

class FuenteCambiada(Exception):
    """La pestaña trabaja con una fuente que ya no es la activa (se cambió en otra pestaña)."""


def _fuente_publica(fte: Dict[str, Any]) -> Dict[str, Any]:
    return {k: fte.get(k) for k in ("id", "nombre", "color")}


def fuentes_listar() -> Dict[str, Any]:
    cfg = load_config()
    items = []
    for f in cfg["fuentes"]:
        fi = indexador.folder_info(f["id"])
        total = 0
        if os.path.exists(config.index_db_path(f["id"])):
            try:
                conn = get_conn(f["id"])
                try:
                    total = conn.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
        items.append(dict(f, folder_display=fi["display"], existe=fi["exists"], total_docs=total))
    return {"activa": cfg["fuente_activa"], "items": items}


def fuente_activar(fid: str) -> Dict[str, Any]:
    cfg = load_config()
    fuente(cfg, fid)  # valida
    if cfg["fuente_activa"] != fid:
        cfg["fuente_activa"] = fid
        save_config(cfg)
    indexador.start_sync_async(esperar=True, fid=fid)
    return query_info()


def fuente_crear(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    f = config.nueva_fuente(cfg, str(payload.get("nombre", "")), str(payload.get("biblioteca", "")))
    save_config(cfg)
    if f["biblioteca"]:
        indexador.start_sync_async(esperar=True, fid=f["id"])
    return f


def fuente_modificar(fid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    f = fuente(cfg, fid)
    if "nombre" in payload:
        nombre = " ".join(str(payload["nombre"] or "").split())[:30]
        if not nombre:
            raise ValueError("El nombre de la fuente está vacío.")
        if any(x["id"] != fid and x["nombre"].lower() == nombre.lower() for x in cfg["fuentes"]):
            raise ValueError(f"Ya existe una fuente llamada «{nombre}».")
        f["nombre"] = nombre
    if "biblioteca" in payload:
        f["biblioteca"] = str(payload["biblioteca"] or "").strip()
    if "include_subfolders" in payload:
        f["include_subfolders"] = bool(payload["include_subfolders"])
    if isinstance(payload.get("excluded_dirs"), list):
        f["excluded_dirs"] = [str(x).strip() for x in payload["excluded_dirs"] if str(x).strip()]
    save_config(cfg)
    indexador.start_sync_async(esperar=True, fid=fid)
    return f


def fuente_quitar(fid: str) -> Dict[str, Any]:
    """Quita una fuente: borra su índice (regenerable) y conserva su archivo de etiquetas."""
    if not indexador._SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    try:
        cfg = load_config()
        f = fuente(cfg, fid)
        if fid == cfg["fuente_activa"]:
            raise ValueError("No se puede quitar la fuente activa: cambia antes a otra.")
        if len(cfg["fuentes"]) <= 1:
            raise ValueError("Tiene que quedar al menos una fuente.")
        cfg["fuentes"] = [x for x in cfg["fuentes"] if x["id"] != fid]
        save_config(cfg)
        borrar_indice(fid)
        return {"status": "ok", "nombre": f["nombre"], "etiquetas": os.path.basename(config.etiquetas_db_path(fid))}
    finally:
        indexador._SYNC_LOCK.release()


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
        d["copias"] = [{"id": c["id"], "ruta_rel": c["ruta_rel"], "ruta_display": to_display_path(c["ruta"]), "mtime": c["mtime"]}
                       for c in conn.execute("SELECT id, ruta, ruta_rel, mtime FROM archivos WHERE copia_de = ? ORDER BY mtime",
                                             (d["archivo_id"],))]
    finally:
        conn.close()
    return d


def get_archivo(archivo_id: int) -> Optional[Dict[str, Any]]:
    """Cualquier archivo del índice, también las copias de un duplicado (que no tienen documento)."""
    conn = get_conn()
    try:
        r = conn.execute("SELECT id, ruta, nombre FROM archivos WHERE id = ?", (archivo_id,)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def duplicados_listar() -> Dict[str, Any]:
    conn = get_conn()
    try:
        return duplicados.listar(conn, meta_get(conn, "duplicados_revisados"))
    finally:
        conn.close()


def duplicados_visto() -> Dict[str, Any]:
    """Marca como revisados los duplicados detectados hasta ahora (dejan de salir como «nuevos»)."""
    conn = get_conn()
    try:
        with conn:
            meta_set(conn, "duplicados_revisados", datetime.datetime.now().isoformat(timespec="seconds"))
    finally:
        conn.close()
    return {"status": "ok"}


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


def open_file(path: str) -> None:
    """Abre el archivo con su aplicación predeterminada de Windows (Word, Excel…)."""
    if os.name == "nt":
        os.startfile(os.path.normpath(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["explorer.exe", to_display_path(path)])


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
    remoto = False  # True en el listener de acceso desde Internet (finder/remoto.py)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.path.startswith(("/api/doc/", "/api/heartbeat", "/api/status", "/static/")):
            try:
                sys.stderr.write(f"[FinderJR] {self.command} {self.path}\n")
            except Exception:  # noqa: BLE001
                pass

    def end_headers(self) -> None:
        if self.server.remoto:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _autorizar(self, route: str) -> bool:
        """Listener remoto: exige el token de Cloudflare Access y limita lo que se puede hacer."""
        if not self.server.remoto:
            return True
        try:
            remoto.VERIFICADOR.verificar(self.headers.get("Cf-Access-Jwt-Assertion"), remoto.config_remoto())
            if not remoto.ruta_permitida(self.command, route):
                raise remoto.AccesoDenegado("No disponible desde fuera: hazlo en el PC")
        except remoto.AccesoDenegado as e:
            if route.startswith("/api/"):
                self.send_json({"error": str(e)}, status=e.status)
            else:
                body = (f"<!doctype html><meta charset=utf-8><title>FinderJR</title>"
                        f"<p style='font:16px sans-serif;margin:2em'>FinderJR: {html.escape(str(e))}</p>").encode("utf-8")
                self.send_response(e.status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return False
        return True

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
            if not self._autorizar(route):
                return
            if route == "/api/info":
                return self.send_json({**query_info(), "remoto": self.server.remoto})
            if route == "/api/search":
                return self.send_json(query_documents(params))
            if route == "/api/status":
                return self.send_json(dict(indexador.get_state(), activa=config.fuente_activa_id()))
            if route == "/api/config":
                return self.send_json({"config": load_config(), "folder": indexador.folder_info()})
            if route == "/api/fuentes":
                return self.send_json(fuentes_listar())
            if route == "/api/etiquetas":
                return self.send_json(etiquetas_listar())
            if route == "/api/duplicados":
                return self.send_json(duplicados_listar())

            m = re.match(r"^/api/archivo/(\d+)/pdf$", route)
            if m:  # copias de un duplicado (sin documento propio)
                arch = get_archivo(int(m.group(1)))
                if not arch or not os.path.exists(arch["ruta"]):
                    return self.send_json({"error": "No se encuentra el archivo"}, status=404)
                with open(arch["ruta"], "rb") as f:
                    data = f.read()
                ctype = mimetypes.guess_type(arch["nombre"])[0] or "application/octet-stream"
                return self.send_bytes(data, ctype, filename=arch["nombre"], inline=True)

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
                if not es_pdf(doc["nombre"]):
                    return self.send_json({"error": "Solo los PDF tienen vista de página"}, status=400)
                png = render_page_png(doc, max(1, int(params.get("page", "1"))), [t for t in qs.get("t", []) if t.strip()])
                return self.send_bytes(png, "image/png")

            if route == "/api/export/csv":
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                fid = config.fuente_activa_id()
                return self.send_bytes(csv_export(params), "text/csv; charset=utf-8", f"FinderJR_{fid}_{ts}.csv", inline=False)
            if route == "/api/export/pdf_bundle":
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
                return self.send_bytes(pdf_bundle(params), "application/pdf", f"FinderJR_{config.fuente_activa_id()}_{ts}.pdf", inline=False)

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

    def _comprobar_fuente(self, payload: Dict[str, Any]) -> str:
        """Las operaciones que modifican algo indican la fuente con la que trabaja la pestaña; si ya no es
        la activa (se cambió en otra pestaña), se rechazan para no tocar la fuente equivocada."""
        activa = config.fuente_activa_id()
        pedida = self.headers.get("X-FinderJR-Fuente") or payload.get("fuente")
        if pedida and pedida != activa:
            raise FuenteCambiada()
        return activa

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
            if not self._autorizar(route):
                return
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

            if route == "/api/fuente/activar":
                return self.send_json(fuente_activar(str(payload.get("id", ""))))
            if route == "/api/fuentes":
                return self.send_json(fuente_crear(payload))
            m = re.match(r"^/api/fuentes/([a-z0-9_]+)(/borrar)?$", route)
            if m:
                if m.group(2):
                    return self.send_json(fuente_quitar(m.group(1)))
                return self.send_json(fuente_modificar(m.group(1), payload))

            if route.startswith(("/api/sync", "/api/open_location", "/api/open_file", "/api/etiquet", "/api/doc/", "/api/duplicados")):
                self._comprobar_fuente(payload)

            if route == "/api/sync":
                started = indexador.start_sync_async(force_reindex=bool(payload.get("force_reindex")))
                return self.send_json({"started": started, "estado": indexador.get_state()})

            if route == "/api/open_location":
                if payload.get("archivo_id"):
                    doc = get_archivo(int(payload["archivo_id"]))
                else:
                    doc = get_doc(int(payload.get("doc_id", 0)))
                if not doc:
                    return self.send_json({"error": "Documento no encontrado"}, status=404)
                open_location(doc["ruta"])
                return self.send_json({"status": "ok"})
            if route == "/api/open_file":
                doc = get_doc(int(payload.get("doc_id", 0)))
                if not doc:
                    return self.send_json({"error": "Documento no encontrado"}, status=404)
                if not os.path.exists(doc["ruta"]):
                    return self.send_json({"error": f"No se encuentra el archivo: {doc['ruta_display']}"}, status=404)
                open_file(doc["ruta"])
                return self.send_json({"status": "ok"})

            if route == "/api/duplicados/resolver":
                grupos = payload.get("grupos")
                if not isinstance(grupos, list):
                    raise ValueError("Petición no válida.")
                return self.send_json(indexador.resolver_duplicados(grupos))
            if route == "/api/duplicados/visto":
                return self.send_json(duplicados_visto())

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

            m = re.match(r"^/api/doc/(\d+)/eliminar$", route)
            if m:
                return self.send_json(indexador.eliminar(int(m.group(1)), con_copias=bool(payload.get("copias"))))

            m = re.match(r"^/api/doc/(\d+)/renombrar$", route)
            if m:
                res = indexador.renombrar(int(m.group(1)), str(payload.get("nombre", "")))
                return self.send_json(dict(res, doc=get_doc(int(m.group(1)))))

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

            if route == "/api/config":  # ajustes globales (las carpetas van en /api/fuentes/<id>)
                cfg = load_config()
                if "sync_interval_min" in payload:
                    try:
                        cfg["sync_interval_min"] = max(1, min(240, int(payload["sync_interval_min"])))
                    except Exception:  # noqa: BLE001
                        pass
                save_config(cfg)
                return self.send_json({"status": "ok"})

            self.send_json({"error": "Endpoint POST no válido"}, status=404)
        except FuenteCambiada:
            self.send_json({"error": "La fuente activa ha cambiado en otra pestaña: se recarga esta.",
                            "fuente_cambiada": True}, status=409)
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


def servir_remoto(puerto: int, stop: threading.Event) -> None:
    """Listener de acceso desde Internet. Con la red «mirrored» de WSL el puerto puede quedar reservado
    unos instantes (conexiones del relé en curso al reiniciar), así que se reintenta hasta conseguirlo."""
    aviso = False
    while not stop.is_set():
        try:
            srv = FinderHTTPServer(("127.0.0.1", puerto), Handler)
        except OSError as e:
            if not aviso:
                sys.stderr.write(f"[FinderJR] Puerto remoto {puerto} ocupado ({e}); se reintenta cada 5 s\n")
                aviso = True
            stop.wait(5)
            continue
        srv.remoto = True
        try:
            srv.serve_forever()
        finally:
            srv.server_close()
        return


def main() -> None:
    global SERVER_READY_AT
    port = int(os.environ.get("FINDERJR_PORT", DEFAULT_PORT))
    url = f"http://localhost:{port}"
    if is_port_in_use(port):
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    config.migrar_archivos_legado()  # una sola Biblioteca -> fuentes (renombra índice y etiquetas)
    init_db()
    server = FinderHTTPServer(("127.0.0.1", port), Handler)
    SERVER_READY_AT = time.monotonic()

    stop = threading.Event()

    # Acceso desde Internet: segundo listener, también solo en 127.0.0.1 (lo publica el relé del túnel)
    cfg_remoto = load_config().get("remoto") or {}
    if cfg_remoto.get("activo"):
        threading.Thread(target=servir_remoto, args=(int(cfg_remoto.get("puerto") or 8767), stop),
                         daemon=True, name="finder-remoto").start()

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
