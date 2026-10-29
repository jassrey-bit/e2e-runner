"""Catálogo de features/escenarios/tags, generado con `cucumber-js --dry-run`.

El dry-run no abre navegador ni corre hooks, tarda unos segundos y además
marca los pasos sin definir. Se guarda en caché y solo se regenera cuando
cambia algún archivo de features, steps o pages."""
from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from api.config import BASEPROJECT_DIR
from api.cucumber import base_env, build_command, feature_files, message_format
from api.messages import MessageReader

_cache: dict[str, tuple[float, dict]] = {}
_lock = Lock()


def _source_mtime() -> float:
    latest = 0.0
    for sub in ("features", "steps", "pages", "core"):
        for f in (BASEPROJECT_DIR / "src" / sub).rglob("*"):
            if f.suffix in (".feature", ".ts"):
                latest = max(latest, f.stat().st_mtime)
    return latest


def get_catalog(version: str) -> dict:
    with _lock:
        mtime = _source_mtime()
        cached = _cache.get(version)
        if cached and cached[0] == mtime:
            return cached[1]
        catalog = _build(version)
        _cache[version] = (mtime, catalog)
        return catalog


_PARSE_ERROR_RE = re.compile(r'Parse error in "([^"]+)"')


def _build(version: str) -> dict:
    files = feature_files(version)
    invalid: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        ndjson = Path(tmp) / "catalog.ndjson"
        # Un solo .feature mal escrito (por ejemplo una plantilla) hace que
        # Cucumber no cargue ninguno; se excluyen y se vuelve a intentar.
        for _ in range(3):
            if not files:
                break
            ndjson.unlink(missing_ok=True)
            proc = subprocess.run(
                build_command([*files, "--dry-run", "--format", message_format(ndjson)]),
                cwd=BASEPROJECT_DIR,
                env=base_env(version, "catalogo"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            output = proc.stdout + proc.stderr
            broken = {m.replace("\\", "/") for m in _PARSE_ERROR_RE.findall(output)}
            if not broken:
                break
            invalid.extend(sorted(broken))
            files = [f for f in files if f not in broken]

        if not files:
            return {"version": version, "features": [], "tags": [], "invalid_features": invalid, "generated_at": _now()}
        if not ndjson.exists() or ndjson.stat().st_size == 0:
            raise RuntimeError(f"No se pudo leer el catálogo de BaseProject.\n{output[-1500:]}")

        reader = MessageReader(ndjson, Path(tmp) / "shots", secrets=[])
        reader.poll()
        snapshot = reader.snapshot()

    by_feature: dict[str, dict] = {}
    tags: Counter[str] = Counter()
    for sc in snapshot["scenarios"]:
        feature = by_feature.setdefault(
            sc["uri"],
            {
                "uri": sc["uri"],
                "name": sc["feature"],
                # "src/features/v2/Creditos/Amortizado.feature" -> "Creditos"
                "module": _module(sc["uri"], version),
                "scenarios": [],
            },
        )
        undefined = sum(1 for s in sc["steps"] if s["status"] in ("UNDEFINED", "AMBIGUOUS"))
        feature["scenarios"].append(
            {
                "id": f"{sc['uri']}:{sc['line']}",
                "name": sc["name"],
                "line": sc["line"],
                "tags": sc["tags"],
                "steps": sum(1 for s in sc["steps"] if not s["hook"]),
                "undefined_steps": undefined,
            }
        )
        tags.update(sc["tags"])

    features = sorted(by_feature.values(), key=lambda f: (f["module"], f["uri"]))
    return {
        "version": version,
        "features": features,
        "tags": [{"name": n, "count": c} for n, c in tags.most_common()],
        "invalid_features": invalid,
        "generated_at": _now(),
    }


def _module(uri: str, version: str) -> str:
    parts = uri.split("/")
    try:
        idx = parts.index("features") + (2 if version == "v2" else 1)
    except ValueError:
        return "General"
    return parts[idx] if idx < len(parts) - 1 else "General"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
