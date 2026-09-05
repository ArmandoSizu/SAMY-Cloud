/**
 * Copia a static/vendor/ los archivos de terceros que la aplicacion sirve
 * desde su propio origen.
 *
 * Por que no un CDN:
 *
 *   - La politica de seguridad de contenido de produccion no permite
 *     scripts de otros origenes.
 *   - Una caja registradora tiene que abrir aunque el CDN este caido o la
 *     conexion sea mala. La red del comercio es la que es.
 *   - Un archivo servido por un tercero puede cambiar sin aviso. Aqui la
 *     version queda anclada en package-lock.json y en el repositorio.
 *
 * Ejecutar con:  npm run vendor
 */

import { copyFileSync, mkdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const dest = join(root, "static", "vendor");

/** origen dentro de node_modules -> nombre con el que se sirve */
const ASSETS = [
  ["htmx.org/dist/htmx.min.js", "htmx.min.js"],
  ["alpinejs/dist/cdn.min.js", "alpine.min.js"],
  // Respaldo del lector de codigos. Solo lo descargan los navegadores que no
  // traen BarcodeDetector (hoy, Safari de iOS).
  ["@zxing/library/umd/index.min.js", "zxing.min.js"],
];

mkdirSync(dest, { recursive: true });

let fallos = 0;
for (const [from, to] of ASSETS) {
  const src = join(root, "node_modules", from);
  try {
    copyFileSync(src, join(dest, to));
    const kb = Math.round(statSync(src).size / 1024);
    console.log(`  ok    ${to.padEnd(16)} ${String(kb).padStart(5)} KB   <- ${from}`);
  } catch (error) {
    fallos += 1;
    console.error(`  FALLA ${to}: ${error.message}`);
  }
}

if (fallos > 0) {
  console.error(`\n${fallos} archivo(s) no se copiaron. Ejecuta "npm install" primero.`);
  process.exit(1);
}
console.log("\nListo. Recuerda: python manage.py collectstatic");
