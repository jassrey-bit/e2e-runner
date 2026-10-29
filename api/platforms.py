"""Plataformas que se pueden elegir en la web.

- Por defecto: las de E2E_PLATFORMS (o PLATAFORMA del .env de BaseProject).
  Las administra quien opera el servidor y no se borran desde la web.
- Del equipo: las que se agregan desde la web. Se guardan en
  storage/platforms.json para que todo el equipo las vea.

BaseProject arma la URL como https://{plataforma}.cloudgsf.com, así que solo
se acepta el subdominio y se verifica que responda antes de usarlo."""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from threading import Lock

from api.config import ROOT_DIR, available_platforms

PLATFORM_DOMAIN = os.getenv("E2E_PLATFORM_DOMAIN", "cloudgsf.com")
_STORE = ROOT_DIR / "storage" / "platforms.json"
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_lock = Lock()


class PlatformError(ValueError):
    pass


def _load_team() -> list[dict]:
    if not _STORE.exists():
        return []
    try:
        data = json.loads(_STORE.read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, dict) and p.get("name")]
    except ValueError:
        return []


def _save_team(items: list[dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def list_platforms() -> list[dict]:
    defaults = available_platforms()
    items = [{"name": p, "source": "default", "added_at": None} for p in defaults]
    with _lock:
        team = _load_team()
    items += [
        {"name": p["name"], "source": "team", "added_at": p.get("added_at")}
        for p in team
        if p["name"] not in defaults
    ]
    return items


def known_names() -> set[str]:
    return {p["name"] for p in list_platforms()}


def platform_url(name: str) -> str:
    return f"https://{name}.{PLATFORM_DOMAIN}"


def normalize(name: str) -> str:
    """Acepta que peguen la URL completa ("https://x.cloudgsf.com/Login") y
    deja solo el subdominio."""
    value = name.strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0]
    suffix = f".{PLATFORM_DOMAIN}"
    if value.endswith(suffix):
        value = value[: -len(suffix)]
    return value


def validate(name: str) -> str:
    """Formato + que la plataforma responda. Devuelve el nombre normalizado."""
    value = normalize(name)
    if not _NAME_RE.match(value):
        raise PlatformError(
            f"«{name}» no es un subdominio válido: usa solo letras minúsculas, números y guiones (ej. beta-accede)."
        )
    url = platform_url(value)
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "e2e-runner"})
    try:
        with urllib.request.urlopen(request, timeout=10, context=ssl.create_default_context()):
            pass
    except urllib.error.HTTPError:
        # Respondió (aunque sea con 401/403/500): el servidor existe.
        pass
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlatformError(f"{url} no responde. Revisa que el nombre esté bien escrito y que la plataforma esté encendida.")
    return value


def add_platform(name: str) -> dict:
    value = validate(name)
    with _lock:
        if value in available_platforms() or any(p["name"] == value for p in _load_team()):
            raise PlatformError(f"«{value}» ya está en la lista.")
        item = {"name": value, "added_at": datetime.now(timezone.utc).isoformat()}
        _save_team([*_load_team(), item])
    return {**item, "source": "team"}


def remove_platform(name: str) -> bool:
    if name in available_platforms():
        raise PlatformError(f"«{name}» es una plataforma por defecto; se quita desde E2E_PLATFORMS en el servidor.")
    with _lock:
        team = _load_team()
        remaining = [p for p in team if p["name"] != name]
        if len(remaining) == len(team):
            return False
        _save_team(remaining)
    return True
