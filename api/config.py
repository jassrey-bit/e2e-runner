"""Configuración del runner. Todo sale de variables de entorno (o del .env
de esta carpeta) para que el mismo código sirva en local y en servidor."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

BASEPROJECT_DIR = Path(os.getenv("BASEPROJECT_DIR", str(ROOT_DIR.parent / "BaseProject"))).resolve()
STORAGE_DIR = ROOT_DIR / "storage" / "runs"

VERSIONS = ["v2", "v3"]
DEFAULT_VERSION = os.getenv("E2E_DEFAULT_VERSION", "v2")

# Cuántas corridas pueden estar ejecutándose a la vez (cada una puede abrir
# varios Chromium si se lanzan plataformas en paralelo).
MAX_PARALLEL_RUNS = int(os.getenv("E2E_MAX_PARALLEL_RUNS", "2"))

# Red de seguridad: una corrida que pasa de este tiempo se cancela sola.
RUN_TIMEOUT = timedelta(minutes=int(os.getenv("E2E_RUN_TIMEOUT_MIN", "120")))

RETENTION_PERIOD = timedelta(days=int(os.getenv("E2E_RETENTION_DAYS", "5")))


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def available_platforms() -> list[str]:
    """Plataformas que se pueden elegir en el front. Si el runner no define
    E2E_PLATFORMS se usan las del .env de BaseProject."""
    platforms = _split(os.getenv("E2E_PLATFORMS"))
    if not platforms:
        platforms = _split(dotenv_values(BASEPROJECT_DIR / ".env").get("PLATAFORMA"))
    return platforms


def secret_values() -> list[str]:
    """Valores que nunca deben verse en logs ni en el texto de los pasos:
    los de E2E_MASK_VALUES y los de cualquier variable cuyo nombre parezca
    un secreto."""
    values = _split(os.getenv("E2E_MASK_VALUES"))
    for name, value in os.environ.items():
        if value and len(value) >= 4 and any(k in name.upper() for k in ("PASS", "SECRET", "TOKEN")):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)
