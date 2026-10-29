# e2e-runner

Servicio FastAPI que ejecuta las pruebas E2E de **BaseProject** (Cucumber + Playwright) para
**qa-platform-web** (módulo "Automatización E2E"). Usa el mismo patrón de jobs que los backends de
Validación de documentos y Validación de API: `POST` crea la corrida y el front consulta su estado por polling.

## Arranque

```bash
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
venv\Scripts\python -m uvicorn api.app:app --host 127.0.0.1 --port 8004
```

Configuración opcional en `.env` (ver `.env.example`). Por defecto busca BaseProject en `../BaseProject`
y toma las plataformas de `PLATAFORMA` del `.env` de BaseProject.

## Cómo funciona

- Cada corrida lanza un proceso `cucumber-js` por plataforma con `CI=true` (navegador oculto),
  `--format message` (eventos en NDJSON que se leen mientras se escriben) y `E2E_LIVE_DIR`
  (captura tras cada paso, hecha por `src/core/support/live.hooks.ts` en BaseProject).
- Cancelar mata el árbol completo de procesos (node + Chromium) y conserva lo que alcanzó a correr.
- Todo queda en `storage/runs/{job_id}/` y se borra a los 5 días.
- Plataformas: las de `E2E_PLATFORMS` son fijas; las que agrega el equipo desde la web se guardan en
  `storage/platforms.json`. `POST /runs` también acepta una plataforma que no esté en la lista
  ("Otra plataforma" en la web) si tiene formato válido y responde.

## Endpoints

| Método | Ruta | Uso |
|---|---|---|
| GET | `/platforms` | Plataformas (por defecto + del equipo) y versiones |
| POST | `/platforms` | Agrega una plataforma del equipo (valida formato y que responda) |
| DELETE | `/platforms/{nombre}` | Quita una plataforma del equipo |
| GET | `/catalog?version=v2` | Features, escenarios y tags (dry-run, en caché) |
| POST | `/runs` | Inicia una corrida |
| GET | `/runs` | Historial con filtros |
| GET | `/runs/{id}` | Estado, avance en vivo y resultado |
| POST | `/runs/{id}/cancel` | Cancela la corrida |
| POST | `/runs/{id}/rerun` | Repite la corrida |
| GET | `/runs/{id}/logs/{plataforma}?offset=` | Consola de Cucumber |
| GET | `/runs/{id}/live/{plataforma}` | Última captura en vivo |
| GET | `/runs/{id}/screenshots/{plataforma}/{n}.png` | Captura de un paso fallido |
