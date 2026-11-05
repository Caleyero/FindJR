# -*- coding: utf-8 -*-
"""
Acceso desde Internet (Cloudflare Tunnel + Cloudflare Access).

FinderJR abre un segundo listener en 127.0.0.1:<remoto.puerto>; todo lo que entra por él es remoto.
Cada petición debe traer el JWT que Cloudflare Access añade en la cabecera Cf-Access-Jwt-Assertion:
se verifica aquí (RS256 con la stdlib, sin dependencias) contra las claves públicas del equipo de
Access. Si algo falla, se deniega (falla cerrado).

Desde fuera solo se puede consultar y etiquetar: las rutas POST permitidas están en RUTAS_POST_REMOTAS.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

from . import config

# Rutas POST permitidas desde fuera (todo GET es de lectura y se permite)
RUTAS_POST_REMOTAS = {
    "/api/heartbeat",
    "/api/etiquetar",       # poner / quitar etiquetas
    "/api/etiquetas",       # crear etiqueta (editar o borrar: solo en el PC)
    "/api/fuente/activar",
    "/api/sync",
}

_MARGEN_S = 60          # tolerancia de reloj para exp / nbf / iat
_CACHE_CERTS_S = 3600   # las claves de Access rotan cada pocas semanas

# Prefijo DER de DigestInfo para SHA-256 (RFC 8017, 9.2)
_DIGESTINFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


class AccesoDenegado(Exception):
    def __init__(self, mensaje: str, status: int = 403) -> None:
        super().__init__(mensaje)
        self.status = status


def ruta_permitida(metodo: str, ruta: str) -> bool:
    if metodo == "GET":
        return True
    return metodo == "POST" and ruta in RUTAS_POST_REMOTAS


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _equipo(cfg_remoto: Dict[str, Any]) -> str:
    """Dominio del equipo de Access sin esquema ni barras: «<equipo>.cloudflareaccess.com»."""
    t = str(cfg_remoto.get("cf_team") or "").strip().lower()
    t = re.sub(r"^https?://", "", t).strip("/")
    return t


def _descargar_certs(equipo: str) -> Dict[str, Any]:
    req = urllib.request.Request(f"https://{equipo}/cdn-cgi/access/certs", headers={"User-Agent": "FinderJR"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


class VerificadorAccess:
    """Verifica tokens de Cloudflare Access. `obtener_certs` se puede sustituir en las pruebas."""

    def __init__(self, obtener_certs: Callable[[str], Dict[str, Any]] = _descargar_certs) -> None:
        self._obtener_certs = obtener_certs
        self._lock = threading.Lock()
        self._claves: Dict[str, tuple] = {}
        self._equipo = ""
        self._cargado_en = 0.0

    def _clave(self, equipo: str, kid: str) -> tuple:
        with self._lock:
            caducada = time.monotonic() - self._cargado_en > _CACHE_CERTS_S
            if equipo != self._equipo or caducada or kid not in self._claves:
                # Recarga (como mucho una vez por minuto si se pide un kid desconocido)
                if equipo != self._equipo or caducada or time.monotonic() - self._cargado_en > 60:
                    try:
                        datos = self._obtener_certs(equipo)
                    except Exception as e:  # noqa: BLE001
                        if equipo != self._equipo or not self._claves:
                            raise AccesoDenegado(f"No se pueden obtener las claves de Cloudflare Access: {e}", 503)
                    else:
                        claves = {}
                        for k in datos.get("keys", []):
                            if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
                                claves[k["kid"]] = (int.from_bytes(_b64url(k["n"]), "big"),
                                                    int.from_bytes(_b64url(k["e"]), "big"))
                        self._claves, self._equipo, self._cargado_en = claves, equipo, time.monotonic()
            if kid not in self._claves:
                raise AccesoDenegado("Clave de firma desconocida")
            return self._claves[kid]

    def verificar(self, token: Optional[str], cfg_remoto: Dict[str, Any]) -> str:
        """Devuelve el correo autenticado o lanza AccesoDenegado."""
        equipo = _equipo(cfg_remoto)
        aud_esperada = str(cfg_remoto.get("cf_aud") or "").strip()
        if not equipo or not aud_esperada:
            raise AccesoDenegado("Acceso remoto sin configurar (cf_team / cf_aud)", 503)
        if not token:
            raise AccesoDenegado("Falta la autenticación de Cloudflare Access")
        partes = token.strip().split(".")
        if len(partes) != 3:
            raise AccesoDenegado("Token mal formado")
        try:
            cabecera = json.loads(_b64url(partes[0]))
            datos = json.loads(_b64url(partes[1]))
            firma = _b64url(partes[2])
        except Exception:  # noqa: BLE001
            raise AccesoDenegado("Token mal formado")
        if not isinstance(cabecera, dict) or not isinstance(datos, dict) or cabecera.get("alg") != "RS256":
            raise AccesoDenegado("Algoritmo de firma no admitido")

        n, e = self._clave(equipo, str(cabecera.get("kid") or ""))
        k = (n.bit_length() + 7) // 8
        if len(firma) != k:
            raise AccesoDenegado("Firma no válida")
        em = pow(int.from_bytes(firma, "big"), e, n).to_bytes(k, "big")
        t = _DIGESTINFO_SHA256 + hashlib.sha256(f"{partes[0]}.{partes[1]}".encode("ascii")).digest()
        if k < len(t) + 11:
            raise AccesoDenegado("Firma no válida")
        esperado = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        if not hmac.compare_digest(em, esperado):
            raise AccesoDenegado("Firma no válida")

        ahora = time.time()
        try:
            if float(datos["exp"]) < ahora - _MARGEN_S:
                raise AccesoDenegado("Sesión caducada: recarga la página")
            for campo in ("nbf", "iat"):
                if campo in datos and float(datos[campo]) > ahora + _MARGEN_S:
                    raise AccesoDenegado("Token aún no válido")
        except (KeyError, TypeError, ValueError):
            raise AccesoDenegado("Token sin caducidad")
        aud = datos.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if aud_esperada not in auds:
            raise AccesoDenegado("Token de otra aplicación")
        if str(datos.get("iss") or "").rstrip("/").lower() != f"https://{equipo}":
            raise AccesoDenegado("Emisor no válido")
        correo = str(datos.get("email") or "").strip().lower()
        admitidos = [str(x).strip().lower() for x in (cfg_remoto.get("emails") or []) if str(x).strip()]
        if admitidos and correo not in admitidos:
            raise AccesoDenegado("Usuario no autorizado")
        return correo


VERIFICADOR = VerificadorAccess()


def config_remoto() -> Dict[str, Any]:
    return config.load_config().get("remoto") or {}
