"""Servicio FastAPI que ejecuta las pruebas E2E de BaseProject (Cucumber +
Playwright) para qa-platform-web, con el mismo contrato de job
pending/done/error por polling que los backends de Validación de documentos
y Validación de API. Agrega el estado `cancelled`."""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.catalog import get_catalog
from api.config import DEFAULT_VERSION, VERSIONS
from api.jobs import (
    RunParams,
    cancel_job,
    get_job,
    list_jobs,
    load_persisted_jobs,
    platform_snapshot,
    read_log,
    rerun_job,
    status_label,
    submit_run,
    summary,
)
from api.platforms import PlatformError, add_platform, known_names, list_platforms, remove_platform, validate

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
_SCENARIO_RE = re.compile(r"^src/features/[\w\-./ ]+\.feature(:\d+)?$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_persisted_jobs()
    yield


app = FastAPI(title="E2E Runner Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateRunRequest(BaseModel):
    version: str = DEFAULT_VERSION
    platforms: list[str] = Field(min_length=1)
    scenarios: list[str] = Field(default_factory=list)
    tags: str | None = None
    parallel: bool = True
    live_screenshots: bool = True


def _job_or_404(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="corrida no encontrada")
    return job


def _platform_or_404(job, platform: str):
    if platform not in job.params.platforms:
        raise HTTPException(status_code=404, detail="plataforma no encontrada en la corrida")


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class AddPlatformRequest(BaseModel):
    name: str


@app.get("/platforms")
def platforms() -> dict:
    items = list_platforms()
    return {
        "platforms": [p["name"] for p in items],
        "items": items,
        "versions": VERSIONS,
        "default_version": DEFAULT_VERSION,
    }


@app.post("/platforms")
def create_platform(body: AddPlatformRequest) -> dict:
    # Síncrona: la verificación de que la plataforma responde hace una
    # petición HTTP y corre en el threadpool de FastAPI.
    try:
        return add_platform(body.name)
    except PlatformError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.delete("/platforms/{name}")
def delete_platform(name: str) -> dict:
    try:
        removed = remove_platform(name)
    except PlatformError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not removed:
        raise HTTPException(status_code=404, detail="plataforma no encontrada")
    return {"name": name, "removed": True}


@app.get("/catalog")
def catalog(version: str = DEFAULT_VERSION) -> dict:
    # Ruta síncrona: el dry-run corre en el threadpool de FastAPI.
    if version not in VERSIONS:
        raise HTTPException(status_code=422, detail=f"versión desconocida: {version}")
    try:
        return get_catalog(version)
    except Exception as e:
        logger.exception("Error generando el catálogo %s", version)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/runs")
def create_run(body: CreateRunRequest) -> dict:
    if body.version not in VERSIONS:
        raise HTTPException(status_code=422, detail=f"versión desconocida: {body.version}")
    # Las que no están en la lista se aceptan para esta corrida ("Otra
    # plataforma" en la web) si tienen formato válido y responden.
    known = known_names()
    platforms: list[str] = []
    try:
        for p in body.platforms:
            platforms.append(p if p in known else validate(p))
    except PlatformError as e:
        raise HTTPException(status_code=422, detail=str(e))
    bad = [s for s in body.scenarios if not _SCENARIO_RE.match(s) or ".." in s]
    if bad:
        raise HTTPException(status_code=422, detail=f"escenarios inválidos: {', '.join(bad)}")

    job_id = submit_run(
        RunParams(
            version=body.version,
            platforms=list(dict.fromkeys(platforms)),
            scenarios=body.scenarios,
            tags=(body.tags or "").strip() or None,
            parallel=body.parallel,
            live_screenshots=body.live_screenshots,
        )
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/runs")
def runs(
    status: str | None = None,
    platform: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
) -> dict:
    jobs, total = list_jobs(
        status=status,
        platform=platform,
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        page=page,
        page_size=PAGE_SIZE,
    )
    return {
        "items": [
            {
                "job_id": j.id,
                "created_at": j.created_at.isoformat(),
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "status": status_label(j),
                "version": j.params.version,
                "platforms": j.params.platforms,
                "summary": summary(j),
            }
            for j in jobs
        ],
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
    }


@app.get("/runs/{job_id}")
def get_run(job_id: str) -> dict:
    job = _job_or_404(job_id)
    snaps = [platform_snapshot(job, p) for p in job.platforms]
    return {
        "job_id": job.id,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "params": {
            "version": job.params.version,
            "platforms": job.params.platforms,
            "scenarios": job.params.scenarios,
            "tags": job.params.tags,
            "parallel": job.params.parallel,
            "live_screenshots": job.params.live_screenshots,
        },
        "summary": summary(job, snaps),
        "platforms": snaps,
    }


@app.post("/runs/{job_id}/cancel")
def cancel_run(job_id: str) -> dict:
    job = cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="corrida no encontrada")
    return {"job_id": job.id, "status": job.status, "cancel_requested": job.cancel_requested}


@app.post("/runs/{job_id}/rerun")
def rerun_run(job_id: str) -> dict:
    new_id = rerun_job(job_id)
    if new_id is None:
        raise HTTPException(status_code=404, detail="corrida no encontrada")
    return {"job_id": new_id, "status": "pending"}


@app.get("/runs/{job_id}/logs/{platform}")
def run_logs(job_id: str, platform: str, offset: int = 0) -> dict:
    job = _job_or_404(job_id)
    _platform_or_404(job, platform)
    text, next_offset = read_log(job, platform, max(offset, 0))
    return {"text": text, "offset": next_offset}


@app.get("/runs/{job_id}/live/{platform}")
def live_screenshot(job_id: str, platform: str) -> FileResponse:
    job = _job_or_404(job_id)
    _platform_or_404(job, platform)
    path = job.live_dir(platform) / "latest.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="todavía no hay captura")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/runs/{job_id}/screenshots/{platform}/{name}")
def failure_screenshot(job_id: str, platform: str, name: str) -> FileResponse:
    job = _job_or_404(job_id)
    _platform_or_404(job, platform)
    if not re.fullmatch(r"\d+\.png", name):
        raise HTTPException(status_code=404, detail="captura no encontrada")
    path = job.screenshots_dir(platform) / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="captura no encontrada")
    return FileResponse(path, media_type="image/png")
