// Build-time inventory, not an inference or credential operation.
import { readdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
const files = {};
async function walk(directory) {
  for (const entry of await readdir(directory, {withFileTypes:true})) {
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error('Runtime links are not supported');
    if (entry.isDirectory()) await walk(path);
    else files[path.replaceAll('\\','/')] = createHash('sha256').update(await readFile(path)).digest('hex');
  }
}
await walk('node_modules/openai');
await walk('vendor/local/src');
await walk('vendor/local/dist');
for (const path of ['bridge.mjs', 'package.json', 'package-lock.json', 'project-output.json']) files[path] = createHash('sha256').update(await readFile(path)).digest('hex');
await writeFile('runtime-manifest.json', JSON.stringify({revision:'0a36fefeb913055c8c7a1a29b63d82b96b2e841a', files}, null, 2) + '\n');
