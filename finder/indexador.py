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
import re
import subprocess
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from . import etiquetas
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


# ------------------------------------------------------------------------------
# Renombrado de archivos
# ------------------------------------------------------------------------------

_CARACTERES_PROHIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVADOS = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def validar_nombre(base: str) -> str:
    """Nombre sin extensión válido en Windows; lanza ValueError con un mensaje para el usuario."""
    base = (base or "").strip().rstrip(". ")
    if not base:
        raise ValueError("El nombre no puede estar vacío.")
    m = _CARACTERES_PROHIBIDOS.search(base)
    if m:
        car = m.group(0) if m.group(0).isprintable() else "de control"
        raise ValueError(f"El nombre no puede contener el carácter {car} (prohibidos: \\ / : * ? \" < > |).")
    if base.split(".")[0].upper() in _RESERVADOS:
        raise ValueError(f"«{base}» es un nombre reservado de Windows.")
    if len(base) > 200:
        raise ValueError("El nombre es demasiado largo (máximo 200 caracteres).")
    return base


def renombrar(doc_id: int, nueva_base: str) -> Dict[str, Any]:
    """Renombra en disco el archivo de un documento (conservando la extensión) y actualiza el índice
    y las etiquetas sin volver a extraer el texto. Devuelve el nombre anterior y el nuevo.
    Solo se impide repetir nombre en la misma carpeta (en carpetas distintas es legítimo)."""
    nueva_base = validar_nombre(nueva_base)
    if not _SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    try:
        conn = get_conn()
        try:
            r = conn.execute(
                """SELECT a.id, a.ruta, a.ruta_rel, a.nombre FROM documentos d
                   JOIN archivos a ON a.id = d.archivo_id WHERE d.id = ?""", (doc_id,)).fetchone()
            if not r:
                raise ValueError("Documento no encontrado.")
            viejo, nombre_viejo = r["ruta"], r["nombre"]
            ext = os.path.splitext(nombre_viejo)[1]
            nombre_nuevo = nueva_base + ext
            if nombre_nuevo == nombre_viejo:
                return {"anterior": nombre_viejo, "nombre": nombre_viejo, "cambiado": False}
            if not os.path.exists(viejo):
                raise ValueError("El archivo ya no está en su ruta (¿se ha movido o borrado?). Sincroniza la Biblioteca.")
            nuevo = os.path.join(os.path.dirname(viejo), nombre_nuevo)
            solo_mayusculas = os.path.normcase(nuevo) == os.path.normcase(viejo) or nombre_nuevo.lower() == nombre_viejo.lower()
            if os.path.exists(nuevo) and not solo_mayusculas:
                raise ValueError(f"Ya existe un archivo llamado «{nombre_nuevo}» en esa misma carpeta.")
            if len(to_display_path(nuevo)) > 259:
                raise ValueError("La ruta completa superaría el límite de Windows (260 caracteres). Acorta el nombre.")
            try:
                os.rename(viejo, nuevo)
            except PermissionError:
                raise ValueError("Windows no permite renombrarlo: el archivo está abierto en otro programa "
                                 "(visor PDF, correo…) o no hay permisos. Ciérralo e inténtalo de nuevo.") from None
            except OSError as e:
                raise ValueError(f"No se ha podido renombrar: {e.strerror or e}") from None

            rel_viejo = r["ruta_rel"]
            rel_nuevo = rel_viejo[: len(rel_viejo) - len(nombre_viejo)] + nombre_nuevo
            try:
                with conn:
                    conn.execute("UPDATE archivos SET ruta = ?, ruta_rel = ?, nombre = ? WHERE id = ?",
                                 (nuevo, rel_nuevo, nombre_nuevo, r["id"]))
                    # El trigger documentos_au actualiza el índice de texto completo (nombre y ruta)
                    conn.execute("UPDATE documentos SET nombre = ?, ruta_rel = ? WHERE archivo_id = ?",
                                 (nombre_nuevo, rel_nuevo, r["id"]))
                    conn.execute(f"UPDATE OR REPLACE {etiquetas.ALIAS}.doc_etiqueta SET ruta_rel = ? WHERE ruta_rel = ?",
                                 (etiquetas.clave(rel_nuevo), etiquetas.clave(rel_viejo)))
            except Exception:
                os.rename(nuevo, viejo)  # el índice no se ha podido actualizar: se deja todo como estaba
                raise
            return {"anterior": nombre_viejo, "nombre": nombre_nuevo, "cambiado": True}
        finally:
            conn.close()
    finally:
        _SYNC_LOCK.release()


# ------------------------------------------------------------------------------
# Eliminación (a la Papelera de reciclaje)
# ------------------------------------------------------------------------------

def _a_papelera_windows(path: str) -> None:
    """Envía un archivo a la Papelera con SHFileOperationW (shell32), sin dependencias."""
    import ctypes
    from ctypes import wintypes

    path = os.path.abspath(path)
    raiz = os.path.splitdrive(path)[0] + "\\"
    # Las unidades de red y las rutas UNC no tienen Papelera: Windows borraría definitivamente
    if path.startswith("\\\\") or ctypes.windll.kernel32.GetDriveTypeW(raiz) == 4:  # DRIVE_REMOTE
        raise ValueError("El archivo está en una unidad de red, que no tiene Papelera de reciclaje. "
                         "Elimínalo desde el Explorador si de verdad quieres borrarlo.")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,  # pFrom termina en doble nulo
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperation código {rc}")


def _a_papelera_wsl(path: str) -> None:
    """Desarrollo en WSL: la Papelera de Windows a través de PowerShell."""
    win = to_display_path(path).replace("'", "''")
    cmd = ("Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
           f"'{win}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "").strip()[:300])


def eliminar(doc_id: int) -> Dict[str, Any]:
    """Envía el archivo de un documento a la Papelera de reciclaje y lo quita del índice.

    Las etiquetas se conservan en etiquetas.db: si el archivo se restaura desde la Papelera, la
    siguiente sincronización lo vuelve a indexar y recupera sus etiquetas."""
    if not _SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    try:
        conn = get_conn()
        try:
            r = conn.execute(
                """SELECT a.id, a.ruta, a.nombre FROM documentos d
                   JOIN archivos a ON a.id = d.archivo_id WHERE d.id = ?""", (doc_id,)).fetchone()
            if not r:
                raise ValueError("Documento no encontrado.")
            if os.path.exists(r["ruta"]):
                try:
                    (_a_papelera_windows if os.name == "nt" else _a_papelera_wsl)(r["ruta"])
                except ValueError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise ValueError("Windows no ha podido enviarlo a la Papelera: puede que el archivo esté abierto "
                                     f"en otro programa o que no haya permisos. Ciérralo e inténtalo de nuevo. ({e})") from None
                if os.path.exists(r["ruta"]):
                    raise ValueError("El archivo sigue en su sitio: Windows no ha podido eliminarlo.")
            # (si ya no existía en disco, solo se quita del índice)
            with conn:
                conn.execute("DELETE FROM archivos WHERE id = ?", (r["id"],))  # documentos y FTS en cascada
            return {"status": "ok", "nombre": r["nombre"], "ruta_display": to_display_path(r["ruta"])}
        finally:
            conn.close()
    finally:
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
