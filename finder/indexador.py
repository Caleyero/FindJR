# -*- coding: utf-8 -*-
"""
Indexador incremental de la Biblioteca y proceso en segundo plano.

Ciclo de sincronización:
  1. Descubre archivos nuevos / modificados / eliminados (ruta + tamaño + fecha).
  2. Extrae el texto en paralelo (varios procesos) y lo guarda en lotes en el índice FTS5.
"""

import concurrent.futures as cf
import datetime
import os
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from .config import excluded_dirs, load_config, to_display_path, to_local_path
from .db import get_conn, init_db, meta_set
from .extraccion import extract_pdf_text

_SYNC_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
STATE: Dict[str, Any] = {
    "activo": False, "fase": "inactivo", "total": 0, "hechos": 0, "mensaje": "",
    "inicio": None, "ultimo": None, "error": None,
}


def _set_state(**kw) -> None:
    with _STATE_LOCK:
        STATE.update(kw)


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(STATE)


# ------------------------------------------------------------------------------
# Descubrimiento
# ------------------------------------------------------------------------------

def biblioteca_path(cfg: Dict[str, Any]) -> str:
    return to_local_path(cfg.get("biblioteca", ""))


def discover(cfg: Dict[str, Any]) -> List[Tuple[str, str, int, float]]:
    """[(ruta_local, ruta_rel, size, mtime)] de los archivos indexables."""
    root = biblioteca_path(cfg)
    if not root or not os.path.isdir(root):
        return []
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.get("extensions", [".pdf"])}
    excl = set(excluded_dirs(cfg))
    include_sub = bool(cfg.get("include_subfolders", True))
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in excl and not d.startswith(".")]
        if not include_sub:
            dirs[:] = []
        for fn in files:
            if fn.startswith("~$") or os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(cur, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append((full, os.path.relpath(full, root), st.st_size, round(st.st_mtime, 2)))
    return out


# ------------------------------------------------------------------------------
# Extracción (se ejecuta en procesos hijos)
# ------------------------------------------------------------------------------

def _extract_worker(args: Tuple[str, int]) -> Dict[str, Any]:
    path, max_pages = args
    try:
        paginas, texto = extract_pdf_text(path, max_pages)
        return {"ok": True, "paginas": paginas, "texto": texto}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _store(conn, item: Tuple[str, str, int, float], er: Dict[str, Any], prev_id: Optional[int]) -> None:
    full, rel, size, mtime = item
    nombre = os.path.basename(full)
    parts = rel.replace("\\", "/").split("/")
    carpeta = parts[0] if len(parts) > 1 else "(raíz)"
    if prev_id:
        conn.execute("DELETE FROM archivos WHERE id = ?", (prev_id,))
    ok = er.get("ok")
    texto = er.get("texto", "") if ok else ""
    sin_texto = 1 if len(texto.strip()) < 60 else 0
    paginas = er.get("paginas", 0) if ok else 0
    cur = conn.execute(
        """INSERT INTO archivos (ruta, ruta_rel, carpeta, nombre, ext, size, mtime, paginas, necesita_ocr, estado, error, indexado_en)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (full, rel, carpeta, nombre, os.path.splitext(nombre)[1].lower(), size, mtime, paginas,
         sin_texto if ok else 0, "OK" if ok else "ERROR", None if ok else er.get("error"),
         datetime.datetime.now().isoformat(timespec="seconds")),
    )
    n_pag = max(1, paginas)
    conn.execute(
        """INSERT INTO documentos (archivo_id, primera_pag, ultima_pag, num_paginas, nombre, ruta_rel, carpeta, mtime, texto, sin_texto)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cur.lastrowid, n_pag, n_pag, nombre, rel, carpeta, mtime, texto, sin_texto),
    )


# ------------------------------------------------------------------------------
# Sincronización
# ------------------------------------------------------------------------------

def sync(force_reindex: bool = False) -> Dict[str, Any]:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"status": "busy"}
    t0 = time.time()
    res: Dict[str, Any] = {"status": "ok", "added": 0, "updated": 0, "removed": 0, "errors": 0}
    try:
        _set_state(activo=True, fase="escaneando", total=0, hechos=0, error=None,
                   mensaje="Explorando la Biblioteca…", inicio=datetime.datetime.now().isoformat(timespec="seconds"))
        init_db()
        cfg = load_config()
        conn = get_conn()

        found = discover(cfg)
        if force_reindex:
            with conn:
                conn.execute("DELETE FROM archivos")
        existing = {r["ruta"]: (r["id"], r["size"], r["mtime"]) for r in conn.execute("SELECT id, ruta, size, mtime FROM archivos")}
        found_paths = {f[0] for f in found}
        removed = [v[0] for k, v in existing.items() if k not in found_paths]
        if removed:
            with conn:
                conn.executemany("DELETE FROM archivos WHERE id = ?", [(i,) for i in removed])
        res["removed"] = len(removed)

        todo = []
        for item in found:
            prev = existing.get(item[0])
            if prev and prev[1] == item[2] and abs(prev[2] - item[3]) < 1.0:
                continue
            todo.append((item, prev[0] if prev else None))
        res["pending"] = len(todo)

        _set_state(fase="extrayendo", total=len(todo), hechos=0,
                   mensaje=f"Indexando {len(todo)} archivos nuevos o modificados…")
        max_pages = int(cfg.get("max_pages_text", 60))
        done = 0
        if todo:
            workers = max(1, min(4, (os.cpu_count() or 2) - 1))
            try:
                pool = cf.ProcessPoolExecutor(max_workers=workers)
            except Exception:  # noqa: BLE001
                pool = None
            try:
                for i in range(0, len(todo), 48):
                    chunk = todo[i:i + 48]
                    args = [(it[0], max_pages) for it, _ in chunk]
                    results = None
                    if pool:
                        try:
                            results = list(pool.map(_extract_worker, args, chunksize=4))
                        except Exception:  # noqa: BLE001
                            pool.shutdown(cancel_futures=True)
                            pool = None
                    if results is None:
                        results = [_extract_worker(a) for a in args]
                    with conn:
                        for (it, prev_id), er in zip(chunk, results):
                            _store(conn, it, er, prev_id)
                            res["updated" if prev_id else "added"] += 1
                            if not er.get("ok"):
                                res["errors"] += 1
                    done += len(chunk)
                    _set_state(hechos=done, mensaje=f"Indexando {done:,}/{len(todo):,} archivos…".replace(",", "."))
            finally:
                if pool:
                    pool.shutdown()

        with conn:
            meta_set(conn, "ultima_sync", datetime.datetime.now().isoformat(timespec="seconds"))
        conn.close()
        res["elapsed_sec"] = round(time.time() - t0, 1)
        _set_state(ultimo=res)
        return res
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set_state(error=str(e))
        res.update(status="error", error=str(e))
        return res
    finally:
        _set_state(activo=False, fase="inactivo", mensaje="")
        _SYNC_LOCK.release()


def start_sync_async(**kw) -> bool:
    if _SYNC_LOCK.locked():
        return False
    threading.Thread(target=sync, kwargs=kw, daemon=True, name="finder-sync").start()
    return True


def background_loop(stop_event: threading.Event) -> None:
    """Sincroniza al arrancar y después cada `sync_interval_min` minutos."""
    while not stop_event.is_set():
        try:
            sync()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        mins = max(1, int(load_config().get("sync_interval_min", 10)))
        stop_event.wait(mins * 60)


def folder_info() -> Dict[str, Any]:
    cfg = load_config()
    p = biblioteca_path(cfg)
    return {"display": to_display_path(p) or cfg.get("biblioteca", ""), "exists": bool(p) and os.path.isdir(p)}


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(sync(force_reindex="--full" in sys.argv), ensure_ascii=False, indent=2))
