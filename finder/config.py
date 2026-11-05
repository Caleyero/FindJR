# -*- coding: utf-8 -*-
"""
Configuración persistente de FinderJR (finder_config.json) y utilidades de rutas
compatibles con Windows (producción) y WSL (desarrollo).
"""

import json
import os
import re
import threading
import unicodedata
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "finder_config.json")
# Índices (regenerables) y etiquetas (trabajo manual); las variables de entorno sirven para pruebas
DATA_DIR = os.environ.get("FINDERJR_DATA_DIR") or os.path.join(BASE_DIR, "data")
ETIQUETAS_DIR = os.environ.get("FINDERJR_ETIQUETAS_DIR") or BASE_DIR

# Colores de acento de las fuentes (por orden de creación)
COLORES_FUENTE = ["#0f766e", "#1d4ed8", "#7c3aed", "#b45309", "#be123c", "#4d7c0f"]

# Valores de una fuente nueva. Carpetas (nombre de cualquier nivel) que no se indexan: excluded_dirs
_FUENTE_BASE: Dict[str, Any] = {"biblioteca": "", "include_subfolders": True, "excluded_dirs": ["dbGes26_Backup"]}
_LEGADO_BIBLIOTECA = r"C:\Users\ofici\Documents\Biblioteca"

DEFAULT_CONFIG: Dict[str, Any] = {
    # Fuentes de documentos aisladas entre sí: [{id, nombre, color, biblioteca, include_subfolders, excluded_dirs}]
    "fuentes": [],
    "fuente_activa": "",
    "extensions": [".pdf", ".docx", ".xlsx"],
    "sync_interval_min": 10,
    "max_pages_text": 60,
    "ui_layout": {
        "filters_px": 300,
        "grid_fr": 1.0,
        "preview_fr": 1.2,
        "col_fecha_px": 82,
        "col_origen_px": 200,
    },
    # Acceso desde Internet (Cloudflare Tunnel + Access). Segundo listener en 127.0.0.1:puerto; ver finder/remoto.py
    "remoto": {
        "activo": False,
        "puerto": 8767,
        "cf_team": "",   # <equipo>.cloudflareaccess.com
        "cf_aud": "",    # Application Audience (AUD) Tag de la aplicación de Access
        "emails": [],    # correos admitidos (vacío = cualquiera que Access deje pasar)
    },
}

_LOCK = threading.Lock()


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                for k, v in user.items():
                    if k == "dbges_sources":
                        continue  # obsoleto (versión con entidades)
                    if k in ("ui_layout", "remoto") and isinstance(v, dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
    else:
        cfg.setdefault("biblioteca", _LEGADO_BIBLIOTECA)
        _normalizar_fuentes(cfg)
        save_config(cfg)
        return cfg
    if _normalizar_fuentes(cfg):
        save_config(cfg)
    return cfg


# ------------------------------------------------------------------------------
# Fuentes de documentos
# ------------------------------------------------------------------------------

def slug(nombre: str, usados: Optional[List[str]] = None) -> str:
    """Identificador ASCII estable (se usa en los nombres de archivo del índice y las etiquetas)."""
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:30] or "fuente"
    usados = usados or []
    base, n = s, 2
    while s in usados:
        s = f"{base}_{n}"
        n += 1
    return s


def nueva_fuente(cfg: Dict[str, Any], nombre: str, biblioteca: str = "") -> Dict[str, Any]:
    nombre = " ".join((nombre or "").split())[:30]
    if not nombre:
        raise ValueError("El nombre de la fuente está vacío.")
    fuentes = cfg.setdefault("fuentes", [])
    if any(f["nombre"].lower() == nombre.lower() for f in fuentes):
        raise ValueError(f"Ya existe una fuente llamada «{nombre}».")
    usados = [f.get("color") for f in fuentes]
    color = min(COLORES_FUENTE, key=lambda c: (usados.count(c), COLORES_FUENTE.index(c)))
    f = json.loads(json.dumps(_FUENTE_BASE))
    f.update(id=slug(nombre, [x["id"] for x in fuentes]), nombre=nombre, color=color, biblioteca=(biblioteca or "").strip())
    fuentes.append(f)
    return f


def _normalizar_fuentes(cfg: Dict[str, Any]) -> bool:
    """Migra la configuración de una sola Biblioteca a fuentes y completa campos. Devuelve si cambió."""
    cambiado = False
    if not isinstance(cfg.get("fuentes"), list) or not cfg["fuentes"]:
        bib = str(cfg.get("biblioteca") or "").strip()
        nombre = os.path.basename(bib.replace("\\", "/").rstrip("/")).strip() if bib else ""
        nombre = (nombre[:1].upper() + nombre[1:]) if nombre else "Personal"
        cfg["fuentes"] = []
        f = nueva_fuente(cfg, nombre, bib)
        f["include_subfolders"] = bool(cfg.get("include_subfolders", True))
        if isinstance(cfg.get("excluded_dirs"), list):
            f["excluded_dirs"] = cfg["excluded_dirs"]
        nueva_fuente(cfg, "Profesional" if f["id"] != "profesional" else "Personal")
        cfg["fuente_activa"] = f["id"]
        cambiado = True
    for k in ("biblioteca", "include_subfolders", "excluded_dirs"):
        if k in cfg:
            cfg.pop(k)
            cambiado = True
    for i, f in enumerate(cfg["fuentes"]):
        for k, v in _FUENTE_BASE.items():
            if k not in f:
                f[k] = json.loads(json.dumps(v))
                cambiado = True
        if not f.get("color"):
            f["color"] = COLORES_FUENTE[i % len(COLORES_FUENTE)]
            cambiado = True
    if cfg.get("fuente_activa") not in [f["id"] for f in cfg["fuentes"]]:
        cfg["fuente_activa"] = cfg["fuentes"][0]["id"]
        cambiado = True
    return cambiado


def fuente(cfg: Dict[str, Any], fid: Optional[str] = None) -> Dict[str, Any]:
    """La fuente indicada o, sin id, la activa."""
    fid = fid or cfg.get("fuente_activa")
    for f in cfg.get("fuentes", []):
        if f["id"] == fid:
            return f
    raise ValueError("La fuente no existe.")


def fuente_activa_id() -> str:
    return fuente(load_config())["id"]


def index_db_path(fid: str) -> str:
    return os.path.join(DATA_DIR, f"finder_index_{fid}.db")


def etiquetas_db_path(fid: str) -> str:
    return os.path.join(ETIQUETAS_DIR, f"etiquetas_{fid}.db")


def migrar_archivos_legado() -> None:
    """Una sola Biblioteca -> fuentes: el índice y las etiquetas existentes pasan a la primera fuente.
    Se llama al arrancar, antes de abrir ninguna conexión."""
    cfg = load_config()
    fid = cfg["fuentes"][0]["id"]
    pares = [(os.path.join(DATA_DIR, "finder_index.db"), index_db_path(fid)),
             (os.path.join(ETIQUETAS_DIR, "etiquetas.db"), etiquetas_db_path(fid))]
    for viejo, nuevo in pares:
        if os.path.exists(viejo) and not os.path.exists(nuevo):
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(viejo + suf):
                    os.replace(viejo + suf, nuevo + suf)


def save_config(cfg: Dict[str, Any]) -> None:
    with _LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def expand_env(path_str: str) -> str:
    """Expande %APPDATA% y similares; en WSL resuelve APPDATA del usuario de Windows."""
    p = path_str or ""
    if "%APPDATA%" in p.upper() and not os.environ.get("APPDATA"):
        # WSL: deducir la ruta de Windows a partir del directorio del proyecto (/mnt/c/Users/<user>/...)
        m = re.match(r"^/mnt/([a-z])/Users/([^/]+)/", BASE_DIR)
        if m:
            appdata = f"{m.group(1).upper()}:\\Users\\{m.group(2)}\\AppData\\Roaming"
            p = re.sub(r"%APPDATA%", lambda _m: appdata, p, flags=re.I)
    return os.path.expandvars(p)


def to_local_path(path_str: str) -> str:
    """Convierte una ruta de Windows a la ruta accesible por el intérprete actual."""
    p = expand_env((path_str or "").strip().strip('"').strip("'"))
    if os.name == "posix":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"
        return p  # Las rutas UNC no son accesibles desde WSL; se ignoran sin error
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1).upper()}:\\{m.group(2).replace('/', chr(92))}"
    return os.path.normpath(p) if p else p


def to_display_path(path_str: str) -> str:
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path_str or "")
    if m:
        return f"{m.group(1).upper()}:\\{m.group(2).replace('/', chr(92))}"
    return path_str or ""


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def excluded_dirs(fte: Dict[str, Any]) -> List[str]:
    return [str(x).strip().lower() for x in fte.get("excluded_dirs", []) if str(x).strip()]
