import { AfterStep, Before } from '@cucumber/cucumber';
import * as fs from 'fs';
import * as path from 'path';
import { CustomWorld } from './world';

// Integración con el e2e-runner de la plataforma QA. Todo depende de
// variables que solo define el runner; en una ejecución normal
// (ejecutar.js / npx cucumber-js) estos hooks no hacen nada.
const liveDir = process.env.E2E_LIVE_DIR;
const viewport = process.env.E2E_VIEWPORT?.match(/^(\d+)x(\d+)$/);

// Sin ventana Chromium abre en 800x600 y la plataforma cambia a menú
// hamburguesa, así que se fija una resolución de escritorio. Corre después
// del Before de hooks.ts, que es el que crea this.page.
Before(async function (this: CustomWorld) {
  if (!viewport || !this.page) return;
  await this.page.setViewportSize({ width: Number(viewport[1]), height: Number(viewport[2]) });
});

AfterStep(async function (this: CustomWorld, { pickleStep }) {
  if (!liveDir || !this.page) return;

  try {
    fs.mkdirSync(liveDir, { recursive: true });

    const image = await this.page.screenshot({ type: 'jpeg', quality: 60, timeout: 5000 });

    // Se escribe a un temporal y se renombra para que el runner nunca
    // sirva una imagen a medio escribir.
    const tmp = path.join(liveDir, 'latest.tmp');
    fs.writeFileSync(tmp, image);
    fs.renameSync(tmp, path.join(liveDir, 'latest.jpg'));

    fs.writeFileSync(
      path.join(liveDir, 'latest.json'),
      JSON.stringify({ step: pickleStep.text, at: new Date().toISOString() })
    );
  } catch {
    // Una captura fallida nunca debe tumbar el escenario.
  }
});
