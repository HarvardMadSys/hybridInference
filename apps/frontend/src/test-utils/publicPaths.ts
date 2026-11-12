/**
 * The public path manifest, `deploy/public-paths.json`, for the tests that hold
 * the console's routing to it.
 *
 * The manifest names every public path that is not one of the console's own
 * pages, and what serves it. The console implements it in four places that
 * cannot read a file -- route handlers, `rewrites()` and `redirects()` in
 * next.config.js, and the middleware's literal `matcher` -- so each of those
 * is compared with the manifest by a test instead. An entry may set
 * `entry_split`: a deployment's own entry may then send that path straight to
 * the gateway (docs/developer/public-path-table.md).
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export type PublicPath = {
  /** `/v1`; a segment written `:name` stands for any one segment. */
  path: string;
  /** `prefix`: the path itself or anything below it. `exact`: the path alone. */
  match: 'prefix' | 'exact';
  owner: 'gateway' | 'console' | 'cloud-agent' | 'pgadmin';
  entry_split: boolean;
};

export const MANIFEST_FILE = join(__dirname, '../../../../deploy/public-paths.json');

export const PUBLIC_PATHS: readonly PublicPath[] = (
  JSON.parse(readFileSync(MANIFEST_FILE, 'utf8')) as { paths: PublicPath[] }
).paths;

/** The first segment of `path`: `internal` for `/internal/verify-admin`. */
export function topSegment(path: string): string {
  return path.split('/')[1];
}

/** Paths an entry covers, each `:name` segment filled in. */
export function samplePaths(entry: PublicPath): string[] {
  const path = entry.path.replace(/:[^/]+/g, 'a');
  return entry.match === 'prefix' ? [path, `${path}/a`, `${path}/a/b`] : [path];
}
