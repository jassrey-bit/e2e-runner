"""Job store en memoria + ThreadPoolExecutor + metadata.json en disco, mismo
patrón que los backends de Validación de documentos y Validación de API.

Una corrida (job) ejecuta un proceso de Cucumber por plataforma, en paralelo
o una tras otra. El avance se calcula leyendo el NDJSON de mensajes que cada
proceso va escribiendo."""
from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from api.config import MAX_PARALLEL_RUNS, RETENTION_PERIOD, RUN_TIMEOUT, STORAGE_DIR, secret_values
from api.catalog import get_catalog
from api.cucumber import base_env, kill_tree, message_format, spawn
from api.messages import MessageReader

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_RUNS)
_jobs: dict[str, "Job"] = {}
_lock = Lock()

_PURGE_CHECK_INTERVAL = timedelta(hours=1)
_last_purge_at: datetime | None = None


@dataclass
class RunParams:
    version: str
    platforms: list[str]
    # "uri:linea" de cada escenario elegido; vacío = todos los de la versión.
    scenarios: list[str] = field(default_factory=list)
    tags: str | None = None
    parallel: bool = True
    live_screenshots: bool = True


@dataclass
class PlatformRun:
    name: str
    state: str = "queued"  # queued | running | done | cancelled | error
    error: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict | None = None  # snapshot final (se persiste)


@dataclass
class Job:
    id: str
    params: RunParams
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "pending"  # pending | done | cancelled | error
    error: str | None = None
    finished_at: datetime | None = None
    platforms: list[PlatformRun] = field(default_factory=list)
    cancel_requested: bool = False
    # Solo en memoria mientras corre.
    readers: dict[str, MessageReader] = field(default_factory=dict, repr=False)
    procs: dict = field(default_factory=dict, repr=False)

    @property
    def dir(self) -> Path:
        return STORAGE_DIR / self.id

    def platform_dir(self, name: str) -> Path:
        return self.dir / name

    def live_dir(self, name: str) -> Path:
        return self.platform_dir(name) / "live"

    def screenshots_dir(self, name: str) -> Path:
        return self.platform_dir(name) / "screenshots"

    def log_path(self, name: str) -> Path:
        return self.platform_dir(name) / "run.log"

    def ndjson_path(self, name: str) -> Path:
        return self.platform_dir(name) / "messages.ndjson"

    @property
    def metadata_path(self) -> Path:
        return self.dir / "metadata.json"


# ---------------------------------------------------------------- persistencia


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _parse(d: str | None) -> datetime | None:
    return datetime.fromisoformat(d) if d else None


def _write_metadata(job: Job) -> None:
    data = {
        "id": job.id,
        "params": asdict(job.params),
        "created_at": _iso(job.created_at),
        "finished_at": _iso(job.finished_at),
        "status": job.status,
        "error": job.error,
        "platforms": [
            {
                "name": p.name,
                "state": p.state,
                "error": p.error,
                "exit_code": p.exit_code,
                "started_at": _iso(p.started_at),
                "finished_at": _iso(p.finished_at),
                "result": p.result,
            }
            for p in job.platforms
        ],
    }
    try:
        job.metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("No se pudo guardar metadata.json para el job %s", job.id)


def _job_from_metadata(data: dict) -> Job:
    job = Job(
        id=data["id"],
        params=RunParams(**data["params"]),
        created_at=_parse(data["created_at"]),
        finished_at=_parse(data.get("finished_at")),
        status=data["status"],
        error=data.get("error"),
        platforms=[
            PlatformRun(
                name=p["name"],
                state=p["state"],
                error=p.get("error"),
                exit_code=p.get("exit_code"),
                started_at=_parse(p.get("started_at")),
                finished_at=_parse(p.get("finished_at")),
                result=p.get("result"),
            )
            for p in data.get("platforms", [])
        ],
    )
    if job.status == "pending":
        # El proceso que la ejecutaba murió con el servicio anterior.
        job.status = "error"
        job.error = "Ejecución interrumpida por un reinicio del servicio."
        for p in job.platforms:
            if p.state in ("queued", "running"):
                p.state = "error"
                if p.result is None and job.ndjson_path(p.name).exists():
                    reader = MessageReader(job.ndjson_path(p.name), job.screenshots_dir(p.name), secret_values())
                    reader.poll()
                    p.result = reader.snapshot(cancelled=True)
    return job


def load_persisted_jobs() -> None:
    if not STORAGE_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - RETENTION_PERIOD
    loaded: dict[str, Job] = {}
    for job_dir in STORAGE_DIR.iterdir():
        metadata_path = job_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            job = _job_from_metadata(json.loads(metadata_path.read_text(encoding="utf-8")))
        except Exception:
            logger.exception("metadata.json inválido en %s, se omite", job_dir)
            continue
        if job.created_at < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)
            continue
        _write_metadata(job)
        loaded[job.id] = job
    with _lock:
        _jobs.update(loaded)


def _purge_expired_jobs() -> None:
    global _last_purge_at
    now = datetime.now(timezone.utc)
    with _lock:
        if _last_purge_at is not None and now - _last_purge_at < _PURGE_CHECK_INTERVAL:
            return
        _last_purge_at = now
        cutoff = now - RETENTION_PERIOD
        expired = [j for j in _jobs.values() if j.created_at < cutoff and j.status != "pending"]
        for j in expired:
            del _jobs[j.id]
    for j in expired:
        shutil.rmtree(j.dir, ignore_errors=True)


# ---------------------------------------------------------------- ejecución


def submit_run(params: RunParams) -> str:
    _purge_expired_jobs()
    job = Job(id=uuid.uuid4().hex, params=params, platforms=[PlatformRun(name=p) for p in params.platforms])
    job.dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        _jobs[job.id] = job
    _write_metadata(job)
    _executor.submit(_run_job, job)
    return job.id


def _cucumber_args(job: Job, platform: str) -> list[str]:
    params = job.params
    # Sin selección = todos los features válidos de la versión (el catálogo ya
    # descartó los que no se pueden leer).
    targets = params.scenarios or [f["uri"] for f in get_catalog(params.version)["features"]]
    args = [*targets, "--format", message_format(job.ndjson_path(platform))]
    if params.tags:
        args += ["--tags", params.tags]
    return args


def _start_platform(job: Job, run: PlatformRun) -> None:
    pdir = job.platform_dir(run.name)
    pdir.mkdir(parents=True, exist_ok=True)
    env = base_env(job.params.version, run.name)
    if job.params.live_screenshots:
        env["E2E_LIVE_DIR"] = str(job.live_dir(run.name))
    job.readers[run.name] = MessageReader(job.ndjson_path(run.name), job.screenshots_dir(run.name), secret_values())
    job.procs[run.name] = spawn(_cucumber_args(job, run.name), env, job.log_path(run.name))
    run.state = "running"
    run.started_at = datetime.now(timezone.utc)


def _finish_platform(job: Job, run: PlatformRun) -> None:
    proc = job.procs.get(run.name)
    reader = job.readers.get(run.name)
    run.exit_code = proc.returncode if proc else None
    run.finished_at = datetime.now(timezone.utc)
    if reader is not None:
        reader.poll()
        run.result = reader.snapshot(cancelled=job.cancel_requested)
    if job.cancel_requested:
        run.state = "cancelled"
    elif run.result is None or (run.result["total"] == 0 and run.exit_code not in (0, None)):
        # Cucumber ni siquiera llegó a correr escenarios: error de arranque
        # (TypeScript que no compila, feature inválido, etc.).
        run.state = "error"
        run.error = _log_tail(job, run.name) or f"Cucumber terminó con código {run.exit_code}."
    else:
        run.state = "done"


def _run_job(job: Job) -> None:
    deadline = time.monotonic() + RUN_TIMEOUT.total_seconds()
    queue = list(job.platforms)
    try:
        while True:
            running = [p for p in job.platforms if p.state == "running"]

            if job.cancel_requested:
                for p in running:
                    kill_tree(job.procs[p.name])
                for p in queue:
                    p.state = "cancelled"
                queue.clear()

            # Lanzar las que tocan: todas de golpe en paralelo, o de una en una.
            while queue and not job.cancel_requested and (job.params.parallel or not running):
                p = queue.pop(0)
                try:
                    _start_platform(job, p)
                    running.append(p)
                except Exception as e:
                    p.state = "error"
                    p.error = str(e)
                if not job.params.parallel:
                    break

            for p in running:
                if job.procs[p.name].poll() is not None:
                    _finish_platform(job, p)
                    _write_metadata(job)

            if not queue and all(p.state not in ("queued", "running") for p in job.platforms):
                break

            if time.monotonic() > deadline and not job.cancel_requested:
                job.cancel_requested = True
                job.error = f"Se canceló al superar el tiempo máximo ({int(RUN_TIMEOUT.total_seconds() // 60)} min)."

            time.sleep(1)

        if job.cancel_requested:
            job.status = "cancelled"
        elif all(p.state == "error" for p in job.platforms):
            job.status = "error"
            job.error = job.error or "Ninguna plataforma pudo ejecutarse."
        else:
            job.status = "done"
    except Exception as e:
        logger.exception("Error ejecutando el job %s", job.id)
        for proc in job.procs.values():
            kill_tree(proc)
        job.status = "error"
        job.error = f"Error inesperado: {e}"
    job.finished_at = datetime.now(timezone.utc)
    job.readers.clear()
    job.procs.clear()
    _write_metadata(job)


def cancel_job(job_id: str) -> Job | None:
    job = get_job(job_id)
    if job is not None and job.status == "pending":
        job.cancel_requested = True
    return job


def rerun_job(job_id: str) -> str | None:
    original = get_job(job_id)
    if original is None:
        return None
    return submit_run(RunParams(**asdict(original.params)))


# ---------------------------------------------------------------- consultas


def get_job(job_id: str) -> Job | None:
    _purge_expired_jobs()
    with _lock:
        return _jobs.get(job_id)


def list_jobs(
    *,
    status: str | None = None,
    platform: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Job], int]:
    _purge_expired_jobs()
    with _lock:
        jobs = list(_jobs.values())
    if status:
        jobs = [j for j in jobs if status_label(j) == status.upper()]
    if platform:
        jobs = [j for j in jobs if platform in j.params.platforms]
    if date_from:
        jobs = [j for j in jobs if j.created_at.date() >= date_from]
    if date_to:
        jobs = [j for j in jobs if j.created_at.date() <= date_to]
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    start = max(page - 1, 0) * page_size
    return jobs[start : start + page_size], len(jobs)


def platform_snapshot(job: Job, run: PlatformRun) -> dict:
    reader = job.readers.get(run.name)
    if run.state == "running" and reader is not None:
        reader.poll()
        data = reader.snapshot()
    else:
        data = run.result or {"total": 0, "finished": 0, "passed": 0, "failed": 0, "current_scenario_id": None, "scenarios": []}
    live = None
    meta = job.live_dir(run.name) / "latest.json"
    if meta.exists():
        try:
            live = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            live = None
    return {
        "name": run.name,
        "state": run.state,
        "error": run.error,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "live": live,
        **data,
    }


def summary(job: Job, snapshots: list[dict] | None = None) -> dict:
    snaps = snapshots if snapshots is not None else [platform_snapshot(job, p) for p in job.platforms]
    return {
        "total": sum(s["total"] for s in snaps),
        "finished": sum(s["finished"] for s in snaps),
        "passed": sum(s["passed"] for s in snaps),
        "failed": sum(s["failed"] for s in snaps),
    }


def status_label(job: Job) -> str:
    """PASSED / FAILED / CANCELLED / ERROR / RUNNING, para filtrar el historial."""
    if job.status == "pending":
        return "RUNNING"
    if job.status in ("cancelled", "error"):
        return job.status.upper()
    s = summary(job)
    return "PASSED" if s["failed"] == 0 and all(p.state == "done" for p in job.platforms) else "FAILED"


def read_log(job: Job, platform: str, offset: int, limit: int = 64_000) -> tuple[str, int]:
    path = job.log_path(platform)
    if not path.exists():
        return "", offset
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(limit)
    text = chunk.decode("utf-8", errors="replace")
    for s in secret_values():
        text = text.replace(s, "••••••")
    return text, offset + len(chunk)


def _log_tail(job: Job, platform: str, size: int = 2000) -> str:
    path = job.log_path(platform)
    if not path.exists():
        return ""
    data = path.read_bytes()[-size:].decode("utf-8", errors="replace").strip()
    for s in secret_values():
        data = data.replace(s, "••••••")
    return data
