import path from 'path';
import { pathToFileURL } from 'url';

async function main() {
  const payloadRaw = process.argv[2] || '{}';
  const payload = JSON.parse(payloadRaw);
  const modulePath = path.resolve(payload.modulePath);
  const exportName = payload.exportName;
  const mode = payload.mode || 'call';
  const args = Array.isArray(payload.args) ? payload.args : [];

  const mod = await import(pathToFileURL(modulePath).href);
  if (!(exportName in mod)) {
    throw new Error(`Export '${exportName}' not found in ${modulePath}`);
  }

  const value = mod[exportName];
  if (mode === 'inspect') {
    const type = typeof value;
    if (type === 'function') {
      process.stdout.write(JSON.stringify({ ok: true, type: 'function' }));
      return;
    }
    process.stdout.write(JSON.stringify({ ok: true, type, value }));
    return;
  }

  if (typeof value !== 'function') {
    process.stdout.write(JSON.stringify({ ok: true, value }));
    return;
  }

  const result = await value(...args);
  process.stdout.write(JSON.stringify({ ok: true, value: result }));
}

main().catch((error) => {
  process.stderr.write(JSON.stringify({ ok: false, error: error?.message || String(error) }));
  process.exit(1);
});
