"""Lee, mientras se escribe, el NDJSON que genera `cucumber-js --format message`
y lo convierte en el estado que ve el front: escenarios, pasos y en qué paso
va la ejecución.

Cucumber emite primero los documentos, pickles y todos los testCase (así el
total se conoce desde el inicio) y luego, en orden, testCaseStarted /
testStepStarted / testStepFinished / attachment / testCaseFinished."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from threading import Lock

# Orden de gravedad para el estado de un escenario.
_SEVERITY = ["PASSED", "SKIPPED", "PENDING", "UNDEFINED", "AMBIGUOUS", "FAILED"]


def _duration_ms(d: dict | None) -> int:
    if not d:
        return 0
    return int(d.get("seconds", 0)) * 1000 + int(d.get("nanos", 0)) // 1_000_000


def _mask(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, "••••••")
    return text


class MessageReader:
    def __init__(self, ndjson_path: Path, screenshots_dir: Path, secrets: list[str]):
        self.path = ndjson_path
        self.screenshots_dir = screenshots_dir
        self.secrets = secrets
        self._offset = 0
        self._partial = b""
        self._lock = Lock()

        self.features: dict[str, str] = {}  # uri -> nombre del feature
        self.keywords: dict[str, str] = {}  # astNodeId del paso -> keyword
        self.pickles: dict[str, dict] = {}
        self.hooks: dict[str, str] = {}  # hookId -> tipo
        self.test_cases: dict[str, dict] = {}
        self.started: dict[str, dict] = {}  # testCaseStartedId -> escenario
        self.order: list[str] = []  # testCaseStartedId en orden de ejecución
        self.finished_run = False
        self._shot_count = 0

    # ------------------------------------------------------------------ lectura

    def poll(self) -> None:
        """Procesa lo que se haya agregado al archivo desde la última vez."""
        with self._lock:
            if not self.path.exists():
                return
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
            if not chunk:
                return
            self._offset += len(chunk)
            data = self._partial + chunk
            lines = data.split(b"\n")
            # La última línea puede estar a medio escribir.
            self._partial = lines.pop()
            for line in lines:
                if line.strip():
                    try:
                        self._handle(json.loads(line))
                    except (ValueError, KeyError):
                        continue

    def _handle(self, envelope: dict) -> None:
        if "gherkinDocument" in envelope:
            self._index_document(envelope["gherkinDocument"])
        elif "pickle" in envelope:
            p = envelope["pickle"]
            self.pickles[p["id"]] = p
        elif "hook" in envelope:
            h = envelope["hook"]
            self.hooks[h["id"]] = h.get("type", "")
        elif "testCase" in envelope:
            tc = envelope["testCase"]
            self.test_cases[tc["id"]] = tc
        elif "testCaseStarted" in envelope:
            self._case_started(envelope["testCaseStarted"])
        elif "testStepStarted" in envelope:
            ev = envelope["testStepStarted"]
            step = self._find_step(ev["testCaseStartedId"], ev["testStepId"])
            if step is not None:
                step["status"] = "RUNNING"
        elif "testStepFinished" in envelope:
            ev = envelope["testStepFinished"]
            step = self._find_step(ev["testCaseStartedId"], ev["testStepId"])
            if step is not None:
                result = ev.get("testStepResult", {})
                step["status"] = result.get("status", "UNKNOWN")
                step["duration_ms"] = _duration_ms(result.get("duration"))
                if result.get("message"):
                    step["error"] = _mask(result["message"], self.secrets)
        elif "attachment" in envelope:
            self._attachment(envelope["attachment"])
        elif "testCaseFinished" in envelope:
            ev = envelope["testCaseFinished"]
            scenario = self.started.get(ev["testCaseStartedId"])
            if scenario is not None:
                scenario["finished"] = True
                scenario["status"] = self._scenario_status(scenario)
                scenario["duration_ms"] = sum(s.get("duration_ms", 0) for s in scenario["steps"])
        elif "testRunFinished" in envelope:
            self.finished_run = True

    def _index_document(self, doc: dict) -> None:
        feature = doc.get("feature") or {}
        uri = doc.get("uri", "").replace("\\", "/")
        self.features[uri] = feature.get("name", uri)

        def walk(children: list[dict]) -> None:
            for child in children:
                node = child.get("background") or child.get("scenario")
                if node:
                    for step in node.get("steps", []):
                        self.keywords[step["id"]] = step.get("keyword", "").strip()
                if child.get("rule"):
                    walk(child["rule"].get("children", []))

        walk(feature.get("children", []))

    def _case_started(self, ev: dict) -> None:
        tc = self.test_cases.get(ev["testCaseId"])
        if tc is None:
            return
        pickle = self.pickles.get(tc["pickleId"], {})
        pickle_steps = {s["id"]: s for s in pickle.get("steps", [])}
        uri = pickle.get("uri", "").replace("\\", "/")

        steps = []
        for ts in tc.get("testSteps", []):
            if "pickleStepId" in ts:
                ps = pickle_steps.get(ts["pickleStepId"], {})
                keyword = self.keywords.get((ps.get("astNodeIds") or [""])[0], "")
                steps.append(
                    {
                        "id": ts["id"],
                        "keyword": keyword,
                        "text": _mask(ps.get("text", ""), self.secrets),
                        "status": "PENDING_RUN",
                        "duration_ms": 0,
                        "hook": False,
                    }
                )
            else:
                hook_type = self.hooks.get(ts.get("hookId", ""), "")
                label = "Preparación (Before)" if "BEFORE" in hook_type else "Cierre (After)"
                steps.append(
                    {"id": ts["id"], "keyword": "", "text": label, "status": "PENDING_RUN", "duration_ms": 0, "hook": True}
                )

        # Un reintento reemplaza al intento anterior del mismo testCase.
        for sid in list(self.order):
            if self.started[sid]["test_case_id"] == tc["id"]:
                self.order.remove(sid)

        self.started[ev["id"]] = {
            "id": ev["id"],
            "test_case_id": tc["id"],
            "feature": self.features.get(uri, uri),
            "uri": uri,
            "line": (pickle.get("location") or {}).get("line"),
            "name": pickle.get("name", ""),
            "tags": [t["name"] for t in pickle.get("tags", [])],
            "status": "RUNNING",
            "finished": False,
            "duration_ms": 0,
            "steps": steps,
        }
        self.order.append(ev["id"])

    def _find_step(self, started_id: str, step_id: str) -> dict | None:
        scenario = self.started.get(started_id)
        if scenario is None:
            return None
        return next((s for s in scenario["steps"] if s["id"] == step_id), None)

    def _attachment(self, ev: dict) -> None:
        if ev.get("mediaType") != "image/png" or ev.get("contentEncoding") != "BASE64":
            return
        scenario = self.started.get(ev.get("testCaseStartedId", ""))
        if scenario is None:
            return
        step = self._find_step(scenario["id"], ev.get("testStepId", ""))
        if step is None:
            # Si no viene el paso, se asocia al último que falló.
            step = next((s for s in reversed(scenario["steps"]) if s["status"] == "FAILED"), None)
        if step is None:
            return
        self._shot_count += 1
        name = f"{self._shot_count}.png"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        (self.screenshots_dir / name).write_bytes(base64.b64decode(ev["body"]))
        step["screenshot"] = name

    @staticmethod
    def _scenario_status(scenario: dict) -> str:
        statuses = [s["status"] for s in scenario["steps"]]
        worst = "PASSED"
        for st in statuses:
            if st in _SEVERITY and _SEVERITY.index(st) > _SEVERITY.index(worst):
                worst = st
        return worst

    # ------------------------------------------------------------------ salida

    def snapshot(self, *, cancelled: bool = False) -> dict:
        with self._lock:
            scenarios = []
            for sid in self.order:
                sc = dict(self.started[sid])
                sc["steps"] = [dict(s) for s in sc["steps"]]
                if cancelled and not sc["finished"]:
                    sc["status"] = "CANCELLED"
                    for s in sc["steps"]:
                        if s["status"] in ("RUNNING", "PENDING_RUN"):
                            s["status"] = "CANCELLED"
                sc.pop("test_case_id", None)
                scenarios.append(sc)

            finished = [s for s in scenarios if s["finished"]]
            current = next((s for s in reversed(scenarios) if not s["finished"]), None)
            return {
                "total": len(self.test_cases),
                "finished": len(finished),
                "passed": sum(1 for s in finished if s["status"] == "PASSED"),
                "failed": sum(1 for s in finished if s["status"] != "PASSED"),
                "current_scenario_id": current["id"] if current and not cancelled else None,
                "scenarios": scenarios,
            }
