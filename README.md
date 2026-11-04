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

Configuración en `.env` (ver `.env.example`). Requiere Node.js, npm y git en el PATH.

## De dónde salen las pruebas

- **Modo repositorio** (`E2E_REPO_URL` definido): como el checkout de GitHub Actions. El runner mantiene
  un clon del repositorio de BaseProject en `storage/source/repo.git`; en la web se elige la rama y la
  corrida usa el commit que tenía la rama en ese momento (re-ejecutar repite el mismo commit).
  Cada commit se prepara una vez en `storage/source/checkouts/{commit}` y las dependencias se instalan
  solo cuando cambia `package-lock.json` (`storage/source/deps/`). Los hooks de `support/` (captura en
  vivo y tamaño de pantalla) se agregan a cada copia, así que BaseProject no necesita tenerlos.
- **Modo local** (sin `E2E_REPO_URL`): usa la carpeta `BASEPROJECT_DIR` (por defecto `../BaseProject`)
  tal como está, útil para probar cambios antes de subirlos. Ahí `live.hooks.ts` sí debe existir en
  `src/core/support/`.

Si `E2E_PLATFORMS` no está definido, las plataformas por defecto salen de `PLATAFORMA` del `.env` de
BaseProject.

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
| GET | `/source` | Modo (repositorio o local), ramas y rama predeterminada |
| GET | `/platforms` | Plataformas (por defecto + del equipo) y versiones |
| POST | `/platforms` | Agrega una plataforma del equipo (valida formato y que responda) |
| DELETE | `/platforms/{nombre}` | Quita una plataforma del equipo |
| GET | `/catalog?version=v2&branch=` | Features, escenarios y tags de la rama (dry-run, en caché por commit) |
| POST | `/runs` | Inicia una corrida |
| GET | `/runs` | Historial con filtros |
| GET | `/runs/{id}` | Estado, avance en vivo y resultado |
| POST | `/runs/{id}/cancel` | Cancela la corrida |
| POST | `/runs/{id}/rerun` | Repite la corrida |
| GET | `/runs/{id}/logs/{plataforma}?offset=` | Consola de Cucumber |
| GET | `/runs/{id}/live/{plataforma}` | Última captura en vivo |
| GET | `/runs/{id}/screenshots/{plataforma}/{n}.png` | Captura de un paso fallido |
