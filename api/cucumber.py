"""Cómo se lanza Cucumber dentro de BaseProject y cómo se detiene."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from api.config import BASEPROJECT_DIR

CUCUMBER_BIN = BASEPROJECT_DIR / "node_modules" / "@cucumber" / "cucumber" / "bin" / "cucumber.js"
IS_WINDOWS = sys.platform == "win32"


def feature_files(version: str) -> list[str]:
    """Features de cada versión, relativas a BaseProject y con '/'. Igual que
    ejecutar.js: la v2 vive en src/features/v2 y la v3 es todo lo demás."""
    base = BASEPROJECT_DIR / "src" / "features"
    if version == "v2":
        files = (base / "v2").rglob("*.feature")
    else:
        files = (
            f
            for f in base.rglob("*.feature")
            if f.relative_to(base).parts[0] not in ("v2", "hybrid")
        )
    return sorted(f.relative_to(BASEPROJECT_DIR).as_posix() for f in files)


def message_format(path: Path) -> str:
    # Cucumber pide entrecomillar las partes cuando la ruta lleva "C:".
    return f'"message":"{path}"'


def build_command(args: list[str]) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("No se encontró Node.js en el PATH del runner.")
    if not CUCUMBER_BIN.exists():
        raise RuntimeError(f"No se encontró Cucumber en {CUCUMBER_BIN}. ¿Corriste npm install en BaseProject?")
    return [node, str(CUCUMBER_BIN), *args]


def base_env(version: str, platform: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "VERSION": version,
            "PLATAFORMA": platform,
            # hooks.ts lanza Chromium oculto cuando CI=true.
            "CI": "true",
            # Resolución de escritorio para el navegador oculto (live.hooks.ts).
            "E2E_VIEWPORT": os.getenv("E2E_VIEWPORT", "1920x1080"),
            "FORCE_COLOR": "0",
        }
    )
    return env


def spawn(args: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab")
    kwargs: dict = {"cwd": BASEPROJECT_DIR, "env": env, "stdout": log, "stderr": subprocess.STDOUT}
    # Grupo de procesos propio para poder matar node + todos sus Chromium.
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(build_command(args), **kwargs)
    finally:
        log.close()


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
