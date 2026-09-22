import { mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GET, HEAD } from './route';

function request(assetPath: string, method = 'GET') {
  return new NextRequest(`https://freeinference.org${assetPath}`, { method });
}

describe('/site-assets runtime files', () => {
  let temporaryDirectory: string;
  let assetsDirectory: string;

  beforeEach(async () => {
    temporaryDirectory = await mkdtemp(path.join(tmpdir(), 'hybrid-site-assets-'));
    assetsDirectory = path.join(temporaryDirectory, 'public-assets');
    await mkdir(assetsDirectory);
    vi.stubEnv('SITE_ASSETS_DIR', assetsDirectory);
  });

  afterEach(async () => {
    vi.unstubAllEnvs();
    await rm(temporaryDirectory, { recursive: true, force: true });
  });

  it('streams an image with its MIME type and revalidating public cache policy', async () => {
    const logo = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>';
    await writeFile(path.join(assetsDirectory, 'logo.svg'), logo);

    const response = await GET(request('/site-assets/logo.svg'));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe(logo);
    expect(response.headers.get('content-type')).toBe('image/svg+xml');
    expect(response.headers.get('content-length')).toBe(String(Buffer.byteLength(logo)));
    expect(response.headers.get('cache-control')).toBe('public, max-age=300, must-revalidate');
    expect(response.headers.get('etag')).toMatch(/^W\//);
    expect(response.headers.get('last-modified')).toBeTruthy();
    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
    expect(response.headers.get('cross-origin-resource-policy')).toBe('same-origin');
    expect(response.headers.get('content-security-policy')).toBe(
      "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    );
  });

  it('serves nested image files and detects MIME types case-insensitively', async () => {
    await mkdir(path.join(assetsDirectory, 'sponsors'));
    await writeFile(path.join(assetsDirectory, 'sponsors', 'logo.PNG'), Buffer.from([1, 2, 3]));

    const response = await GET(request('/site-assets/sponsors/logo.PNG'));

    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe('image/png');
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  it('answers HEAD with the same metadata and no body', async () => {
    await writeFile(path.join(assetsDirectory, 'favicon.ico'), Buffer.from([0, 1, 2, 3]));

    const response = await HEAD(request('/site-assets/favicon.ico', 'HEAD'));

    expect(response.status).toBe(200);
    expect(response.body).toBeNull();
    expect(response.headers.get('content-type')).toBe('image/x-icon');
    expect(response.headers.get('content-length')).toBe('4');
  });

  it('revalidates an unchanged asset with its ETag', async () => {
    await writeFile(path.join(assetsDirectory, 'logo.svg'), 'logo');
    const first = await GET(request('/site-assets/logo.svg'));

    const response = await GET(
      new NextRequest('https://freeinference.org/site-assets/logo.svg', {
        headers: { 'if-none-match': first.headers.get('etag')! },
      }),
    );

    expect(response.status).toBe(304);
    expect(response.body).toBeNull();
    expect(response.headers.get('content-length')).toBeNull();
  });

  it('reads SITE_ASSETS_DIR at request time', async () => {
    const otherDirectory = path.join(temporaryDirectory, 'other-public-assets');
    await mkdir(otherDirectory);
    await writeFile(path.join(assetsDirectory, 'logo.svg'), 'first');
    await writeFile(path.join(otherDirectory, 'logo.svg'), 'second');

    const first = await GET(request('/site-assets/logo.svg'));
    vi.stubEnv('SITE_ASSETS_DIR', otherDirectory);
    const second = await GET(request('/site-assets/logo.svg'));

    await expect(first.text()).resolves.toBe('first');
    await expect(second.text()).resolves.toBe('second');
  });

  it('answers 404 when the runtime directory is unset or missing', async () => {
    vi.stubEnv('SITE_ASSETS_DIR', '');
    const unset = await GET(request('/site-assets/logo.svg'));
    vi.stubEnv('SITE_ASSETS_DIR', path.join(temporaryDirectory, 'does-not-exist'));
    const missingRoot = await GET(request('/site-assets/logo.svg'));

    expect(unset.status).toBe(404);
    expect(missingRoot.status).toBe(404);
  });

  it('rejects a relative runtime directory', async () => {
    vi.stubEnv('SITE_ASSETS_DIR', 'relative-assets');

    const response = await GET(request('/site-assets/logo.svg'));

    expect(response.status).toBe(404);
  });

  it('answers 404 for a missing file and for a directory', async () => {
    await mkdir(path.join(assetsDirectory, 'directory.png'));

    const missing = await GET(request('/site-assets/missing.svg'));
    const directory = await GET(request('/site-assets/directory.png'));

    expect(missing.status).toBe(404);
    expect(directory.status).toBe(404);
  });

  it('rejects traversal outside the configured directory', async () => {
    await writeFile(path.join(temporaryDirectory, 'secret.svg'), 'not public');

    const response = await GET(request('/site-assets/%2e%2e%2fsecret.svg'));

    expect(response.status).toBe(404);
    await expect(response.text()).resolves.toBe('Not found.');
  });

  it('rejects a symlink whose real path escapes the configured directory', async () => {
    const secret = path.join(temporaryDirectory, 'secret.svg');
    await writeFile(secret, 'not public');
    await symlink(secret, path.join(assetsDirectory, 'logo.svg'));

    const response = await GET(request('/site-assets/logo.svg'));

    expect(response.status).toBe(404);
  });

  it('does not let an image-named symlink disguise a non-image file', async () => {
    const config = path.join(assetsDirectory, 'private-config.yaml');
    await writeFile(config, 'not public');
    await symlink(config, path.join(assetsDirectory, 'logo.svg'));

    const response = await GET(request('/site-assets/logo.svg'));

    expect(response.status).toBe(404);
  });

  it('never serves dotfiles or non-image files from a misplaced directory', async () => {
    await writeFile(path.join(assetsDirectory, '.secret.svg'), 'not public');
    await writeFile(path.join(assetsDirectory, 'site-config.json'), '{"secret":true}');

    const dotfile = await GET(request('/site-assets/.secret.svg'));
    const config = await GET(request('/site-assets/site-config.json'));

    expect(dotfile.status).toBe(404);
    expect(config.status).toBe(404);
  });
});

describe('/site-assets bundled fallback', () => {
  // The UI module's design assets are packaged into the image under
  // `public/site-assets/`, and this route is the only thing that can serve them
  // — it shadows Next's static handling for the whole prefix. Without the
  // fallback the image carries files nothing reaches: the deployment's mount
  // was the only source, so replacing the module's hero and rebuilding changed
  // nothing on the page.
  let cwd: string;
  let bundled: string;

  beforeEach(async () => {
    cwd = await mkdtemp(path.join(tmpdir(), 'hybrid-bundled-assets-'));
    bundled = path.join(cwd, 'public', 'site-assets', 'demo');
    await mkdir(bundled, { recursive: true });
    vi.stubEnv('SITE_ASSETS_DIR', '');
    vi.spyOn(process, 'cwd').mockReturnValue(cwd);
  });

  afterEach(async () => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    await rm(cwd, { recursive: true, force: true });
  });

  it('serves the module asset from the bundle when no deployment directory is set', async () => {
    const hero = 'hero-bytes';
    await writeFile(path.join(bundled, 'hero-ring.png'), hero);

    const response = await GET(request('/site-assets/demo/hero-ring.png'));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe(hero);
    expect(response.headers.get('content-type')).toBe('image/png');
  });

  it('prefers the deployment directory when it has the file', async () => {
    // The deployment's copy is the operator's, so it wins — and the bundled
    // fallback is what serves everything the operator did not supply.
    await writeFile(path.join(bundled, 'hero-ring.png'), 'bundled');
    const deployment = await mkdtemp(path.join(tmpdir(), 'hybrid-deploy-assets-'));
    await writeFile(path.join(deployment, 'hero-ring.png'), 'deployment');
    vi.stubEnv('SITE_ASSETS_DIR', deployment);

    const fromDeployment = await GET(request('/site-assets/hero-ring.png'));
    const fromBundle = await GET(request('/site-assets/demo/hero-ring.png'));

    await expect(fromDeployment.text()).resolves.toBe('deployment');
    await expect(fromBundle.text()).resolves.toBe('bundled');
    await rm(deployment, { recursive: true, force: true });
  });

  it('still refuses a non-image extension in the bundled tree', async () => {
    await writeFile(path.join(bundled, 'site-config.json'), '{"secret":true}');

    const response = await GET(request('/site-assets/demo/site-config.json'));

    expect(response.status).toBe(404);
  });
});
