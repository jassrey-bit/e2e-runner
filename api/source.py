"""De dónde sale BaseProject para cada corrida.

Modo repositorio (E2E_REPO_URL definido): lo mismo que el checkout de GitHub
Actions. El runner mantiene un clon "bare" del repositorio, lo actualiza con
fetch y, por cada commit que se va a correr, arma una copia en
storage/source/checkouts/{commit} con `git archive`. Las dependencias se
instalan una sola vez por package-lock.json y se comparten entre copias
(node_modules es un enlace). A cada copia se le agregan los hooks propios del
runner (support/*.ts: captura en vivo y tamaño de pantalla), así BaseProject
no necesita tenerlos.

Modo local (sin E2E_REPO_URL): se usa BASEPROJECT_DIR tal como está."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Iterator

from api.config import BASEPROJECT_DIR, DEFAULT_BRANCH, MAX_CHECKOUTS, REPO_TOKEN, REPO_URL, SOURCE_DIR, SUPPORT_DIR
from api.cucumber import kill_tree, spawn_command

logger = logging.getLogger(__name__)

REPO_DIR = SOURCE_DIR / "repo.git"
CHECKOUTS_DIR = SOURCE_DIR / "checkouts"
DEPS_DIR = SOURCE_DIR / "deps"
_READY = ".e2e-ready"
_DEPS_REF = ".e2e-deps"
# Subir este número cuando cambie cómo se arma una copia: las que ya están en
# disco con otro valor se vuelven a preparar.
_CHECKOUT_LAYOUT = "2"

_FETCH_TTL = 30  # segundos: varias peticiones seguidas no repiten el fetch
_INSTALL_TIMEOUT = 30 * 60

_BRANCH_RE = re.compile(r"^[\w][\w./-]{0,199}$")

_git_lock = Lock()
_last_fetch = 0.0
_locks: dict[str, Lock] = {}
_locks_lock = Lock()
_in_use: Counter[str] = Counter()


class SourceError(Exception):
    """El código de BaseProject no se pudo obtener o preparar. El mensaje se
    muestra tal cual en la web."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Project:
    dir: Path
    branch: str | None = None
    commit: str | None = None


def repo_mode() -> bool:
    return bool(REPO_URL)


def public_repo_url() -> str:
    # Sin usuario/contraseña si alguien los puso en la URL.
    return re.sub(r"//[^/@]+@", "//", REPO_URL)


def _lock_for(key: str) -> Lock:
    with _locks_lock:
        return _locks.setdefault(key, Lock())


def _mask(text: str) -> str:
    return text.replace(REPO_TOKEN, "••••••") if REPO_TOKEN else text


# ---------------------------------------------------------------- git


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # Nunca preguntar usuario/contraseña: el servicio no tiene a quién.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    if REPO_TOKEN:
        # El token va por variables de entorno, no en la URL ni en la línea de comando.
        basic = base64.b64encode(f"x-access-token:{REPO_TOKEN}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


def _git(*args: str, timeout: int = 300, binary: bool = False, bare: bool = True):
    git = shutil.which("git")
    if git is None:
        raise SourceError("No se encontró git en el PATH del runner.")
    cmd = [git, *(["--git-dir", str(REPO_DIR)] if bare else []), *args]
    try:
        proc = subprocess.run(cmd, env=_git_env(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SourceError(f"git {args[0]} tardó más de {timeout // 60} min.")
    if proc.returncode != 0:
        detail = _mask(proc.stderr.decode("utf-8", errors="replace").strip())
        raise SourceError(f"git {args[0]} falló: {detail[-800:]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")


def fetch(force: bool = False) -> None:
    global _last_fetch
    with _git_lock:
        if not (REPO_DIR / "HEAD").exists():
            shutil.rmtree(REPO_DIR, ignore_errors=True)
            REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "--bare", "--quiet", REPO_URL, str(REPO_DIR), timeout=900, bare=False)
            _last_fetch = time.monotonic()
            return
        if not force and time.monotonic() - _last_fetch < _FETCH_TTL:
            return
        _git("fetch", "--prune", "--quiet", "origin", "+refs/heads/*:refs/heads/*", timeout=600)
        _last_fetch = time.monotonic()


def branches() -> list[dict]:
    """Ramas del repositorio, la de cambios más recientes primero."""
    fetch()
    out = _git(
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)%09%(objectname)%09%(committerdate:iso-strict)%09%(contents:subject)",
        "refs/heads",
    )
    items = []
    for line in out.splitlines():
        name, commit, committed_at, subject = (line.split("\t", 3) + [""] * 4)[:4]
        items.append({"name": name, "commit": commit, "committed_at": committed_at, "subject": subject})
    return items


def default_branch(names: list[str] | None = None) -> str:
    names = names if names is not None else [b["name"] for b in branches()]
    if DEFAULT_BRANCH and DEFAULT_BRANCH in names:
        return DEFAULT_BRANCH
    try:
        head = _git("symbolic-ref", "--short", "HEAD").strip()
    except SourceError:
        head = ""
    if head in names:
        return head
    if not names:
        raise SourceError("El repositorio no tiene ramas.")
    return names[0]


def resolve(branch: str | None) -> tuple[str, str]:
    """(rama, commit) que se va a correr. Sin rama = la predeterminada."""
    fetch()
    branch = branch or default_branch()
    if not _BRANCH_RE.match(branch) or ".." in branch:
        raise SourceError(f"Nombre de rama inválido: {branch}")
    try:
        commit = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").strip()
    except SourceError:
        raise SourceError(f"La rama «{branch}» no existe en el repositorio.")
    return branch, commit


# ---------------------------------------------------------------- copias


@contextmanager
def use(
    branch: str | None = None,
    commit: str | None = None,
    *,
    log: Path | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_phase: Callable[[str], None] = lambda _m: None,
) -> Iterator[Project]:
    """Deja lista la copia del commit (o de la rama) y la protege de la
    limpieza mientras se usa."""
    if not repo_mode():
        yield Project(BASEPROJECT_DIR)
        return
    if commit is None:
        on_phase("Consultando la rama en el repositorio…")
        branch, commit = resolve(branch)
    target = _checkout(commit, log or SOURCE_DIR / "prepare.log", should_stop, on_phase)
    with _locks_lock:
        _in_use[commit] += 1
    try:
        yield Project(target, branch, commit)
    finally:
        with _locks_lock:
            _in_use[commit] -= 1
        _prune()


def _checkout(commit: str, log: Path, should_stop: Callable[[], bool], on_phase: Callable[[str], None]) -> Path:
    target = CHECKOUTS_DIR / commit[:12]
    with _lock_for(f"checkout:{commit}"):
        ready = target / _READY
        if ready.exists() and ready.read_text(encoding="utf-8") == _CHECKOUT_LAYOUT:
            os.utime(ready)
            return target
        _remove_dir(target)
        target.mkdir(parents=True)

        on_phase(f"Descargando la versión {commit[:7]}…")
        try:
            archive = _git("archive", "--format=tar", commit, binary=True, timeout=600)
        except SourceError:
            # Un commit que no está en el clon (por ejemplo, una re-ejecución
            # de algo que ya se borró de la rama) se intenta traer una vez.
            fetch(force=True)
            archive = _git("archive", "--format=tar", commit, binary=True, timeout=600)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, filter="data")

        deps = _install_deps(target, log, should_stop, on_phase)
        _link_dir(target / "node_modules", deps / "node_modules")
        (target / _DEPS_REF).write_text(deps.name, encoding="utf-8")

        _add_support_files(target)

        ready.write_text(_CHECKOUT_LAYOUT, encoding="utf-8")
        return target


def _add_support_files(project: Path) -> None:
    # Cada rama tiene sus hooks en su propia carpeta (src/core/support en V2,
    # src/support en V3) y Cucumber solo carga la que indica su cucumber.js.
    # world.ts marca cuál es; además los hooks del runner lo importan.
    worlds = sorted((project / "src").rglob("world.ts"))
    if not worlds:
        logger.warning("%s no tiene world.ts: sin captura en vivo ni tamaño de pantalla", project.name)
        return
    for f in SUPPORT_DIR.glob("*.ts"):
        shutil.copy2(f, worlds[0].parent / f.name)


def _install_deps(project: Path, log: Path, should_stop: Callable[[], bool], on_phase: Callable[[str], None]) -> Path:
    lock_file = project / "package-lock.json"
    if not lock_file.exists():
        raise SourceError("El repositorio no tiene package-lock.json; no se pueden instalar las dependencias.")
    digest = hashlib.sha256(lock_file.read_bytes() + (project / "package.json").read_bytes()).hexdigest()[:16]
    deps = DEPS_DIR / digest
    with _lock_for(f"deps:{digest}"):
        ready = deps / _READY
        if ready.exists():
            os.utime(ready)
            return deps
        _remove_dir(deps)
        deps.mkdir(parents=True)
        shutil.copy2(lock_file, deps / "package-lock.json")
        shutil.copy2(project / "package.json", deps / "package.json")
        npm = shutil.which("npm")
        if npm is None:
            raise SourceError("No se encontró npm en el PATH del runner.")
        on_phase("Instalando dependencias (solo la primera vez que cambian)…")
        _run([npm, "ci", "--no-audit", "--no-fund"], deps, log, should_stop)
        on_phase("Verificando el navegador de Playwright…")
        _run([npm, "exec", "--no", "--", "playwright", "install", "chromium"], deps, log, should_stop)
        ready.write_text(digest, encoding="utf-8")
        return deps


def _run(cmd: list[str], cwd: Path, log: Path, should_stop: Callable[[], bool]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as f:
        f.write(f"\n$ {' '.join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd))}\n".encode())
    env = dict(os.environ)
    env["CI"] = "true"
    proc = spawn_command(cmd, cwd, env, log)
    deadline = time.monotonic() + _INSTALL_TIMEOUT
    while proc.poll() is None:
        if should_stop():
            kill_tree(proc)
            proc.wait()
            raise Cancelled()
        if time.monotonic() > deadline:
            kill_tree(proc)
            proc.wait()
            raise SourceError(f"{Path(cmd[0]).stem} {cmd[1]} tardó más de {_INSTALL_TIMEOUT // 60} min.")
        time.sleep(0.5)
    if proc.returncode != 0:
        tail = log.read_bytes()[-1500:].decode("utf-8", errors="replace").strip()
        raise SourceError(f"{Path(cmd[0]).stem} {cmd[1]} falló (código {proc.returncode}).\n{_mask(tail)}")


# ---------------------------------------------------------------- limpieza


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        # Junction: no requiere permisos de administrador como un symlink.
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_dir(path: Path) -> None:
    if not path.exists():
        return
    # Primero el enlace de node_modules, para no borrar las dependencias compartidas.
    link = path / "node_modules"
    if link.is_junction() or link.is_symlink():
        os.unlink(link) if link.is_symlink() else os.rmdir(link)
    shutil.rmtree(path, ignore_errors=True)


def _prune() -> None:
    """Conserva las MAX_CHECKOUTS copias más recientes y las dependencias que
    alguna de ellas usa."""
    if not CHECKOUTS_DIR.exists():
        return
    with _locks_lock:
        busy = {c[:12] for c, n in _in_use.items() if n > 0}
    ready = sorted(
        (d for d in CHECKOUTS_DIR.iterdir() if (d / _READY).exists()),
        key=lambda d: (d / _READY).stat().st_mtime,
        reverse=True,
    )
    for d in ready[MAX_CHECKOUTS:]:
        if d.name not in busy:
            _remove_dir(d)
    if not DEPS_DIR.exists():
        return
    used = {
        (d / _DEPS_REF).read_text(encoding="utf-8").strip()
        for d in CHECKOUTS_DIR.iterdir()
        if (d / _DEPS_REF).exists()
    }
    recent = time.time() - 600  # recién instaladas: su copia aún no las registra
    for d in DEPS_DIR.iterdir():
        if (d / _READY).exists() and d.name not in used and (d / _READY).stat().st_mtime < recent:
            shutil.rmtree(d, ignore_errors=True)
