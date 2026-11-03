# -*- coding: utf-8 -*-
"""
Configuración persistente de FinderJR (finder_config.json) y utilidades de rutas
compatibles con Windows (producción) y WSL (desarrollo).
"""

import json
import os
import re
import threading
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "finder_config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
INDEX_DB_PATH = os.path.join(DATA_DIR, "finder_index.db")

DEFAULT_CONFIG: Dict[str, Any] = {
    "biblioteca": r"C:\Users\ofici\Documents\Biblioteca",
    "include_subfolders": True,
    # Carpetas (nombre de cualquier nivel) que no se indexan
    "excluded_dirs": ["dbGes26_Backup"],
    "extensions": [".pdf"],
    "sync_interval_min": 10,
    "max_pages_text": 60,
    "ui_layout": {
        "filters_px": 300,
        "grid_fr": 1.0,
        "preview_fr": 1.2,
        "col_fecha_px": 82,
        "col_origen_px": 200,
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
                    if k == "ui_layout" and isinstance(v, dict):
                        cfg["ui_layout"].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
    else:
        save_config(cfg)
    return cfg


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


def excluded_dirs(cfg: Dict[str, Any]) -> List[str]:
    return [str(x).strip().lower() for x in cfg.get("excluded_dirs", []) if str(x).strip()]
