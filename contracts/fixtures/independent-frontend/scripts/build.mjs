import { cp, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(root, 'src');
const destination = join(root, 'dist');

for (const name of await readdir(source)) {
  const content = await readFile(join(source, name), 'utf8');
  if (content.includes('../../apps') || content.includes('/apps/frontend') || content.includes('@/')) {
    throw new Error(`${name} imports workspace frontend source`);
  }
}

await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(source, destination, { recursive: true });
