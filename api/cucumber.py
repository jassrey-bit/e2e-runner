"""Cómo se lanza Cucumber dentro de una copia de BaseProject y cómo se detiene.
`project` es la carpeta preparada por api.source (la rama elegida o la carpeta
local)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def feature_files(project: Path, version: str) -> list[str]:
    """Features de cada versión, relativas a BaseProject y con '/'. Igual que
    ejecutar.js: la v2 vive en src/features/v2 y la v3 es todo lo demás."""
    base = project / "src" / "features"
    if version == "v2":
        files = (base / "v2").rglob("*.feature")
    else:
        files = (
            f
            for f in base.rglob("*.feature")
            if f.relative_to(base).parts[0] not in ("v2", "hybrid")
        )
    return sorted(f.relative_to(project).as_posix() for f in files)


def message_format(path: Path) -> str:
    # Cucumber pide entrecomillar las partes cuando la ruta lleva "C:".
    return f'"message":"{path}"'


def build_command(project: Path, args: list[str]) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("No se encontró Node.js en el PATH del runner.")
    cucumber_bin = project / "node_modules" / "@cucumber" / "cucumber" / "bin" / "cucumber.js"
    if not cucumber_bin.exists():
        raise RuntimeError(f"No se encontró Cucumber en {cucumber_bin}. ¿Faltan las dependencias (npm install)?")
    return [node, str(cucumber_bin), *args]


def base_env(version: str, platform: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "VERSION": version,
            "PLATAFORMA": platform,
            # hooks.ts lanza Chromium oculto con CI=true en V2 y con HEADLESS=true en V3.
            "CI": "true",
            "HEADLESS": "true",
            # Resolución de escritorio para el navegador oculto (live.hooks.ts).
            "E2E_VIEWPORT": os.getenv("E2E_VIEWPORT", "1920x1080"),
            "FORCE_COLOR": "0",
        }
    )
    return env


def spawn(project: Path, args: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    return spawn_command(build_command(project, args), project, env, log_path)


def spawn_command(command: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log = open(log_path, "ab")
    kwargs: dict = {"cwd": cwd, "env": env, "stdout": log, "stderr": subprocess.STDOUT}
    # Grupo de procesos propio para poder matar node + todos sus Chromium.
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
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
