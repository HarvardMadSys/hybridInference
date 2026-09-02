import { createReadStream } from 'node:fs';
import { realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { Readable } from 'node:stream';

import { NextResponse, type NextRequest } from 'next/server';

const ASSET_PREFIX = '/site-assets';
const CACHE_CONTROL = 'public, max-age=300, must-revalidate';
const MAX_ASSET_SIZE_BYTES = 20 * 1024 * 1024;

// This public route is intentionally limited to the branding image formats it
// needs. SITE_ASSETS_DIR must be a dedicated public-assets directory, but the
// allowlist also prevents a misplaced config, key, or HTML file being served.
const MIME_TYPES: Readonly<Record<string, string>> = {
  '.avif': 'image/avif',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
};

type AssetFile = {
  path: string;
  size: number;
  mtime: Date;
  etag: string;
  contentType: string;
};

function notFound(): NextResponse {
  return new NextResponse('Not found.', {
    status: 404,
    headers: {
      'cache-control': 'no-store',
      'content-type': 'text/plain; charset=utf-8',
    },
  });
}

function containedBy(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return (
    relative === '' ||
    (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
  );
}

function requestedPath(request: NextRequest): { relativePath: string; contentType: string } | null {
  const pathname = request.nextUrl.pathname;
  if (!pathname.startsWith(`${ASSET_PREFIX}/`)) return null;

  try {
    const relativePath = decodeURIComponent(pathname.slice(ASSET_PREFIX.length + 1));
    const parts = relativePath.split('/');
    if (
      !relativePath ||
      relativePath.includes('\0') ||
      relativePath.includes('\\') ||
      parts.some((part) => !part || part === '.' || part === '..' || part.startsWith('.'))
    ) {
      return null;
    }

    const contentType = MIME_TYPES[path.extname(relativePath).toLowerCase()];
    return contentType ? { relativePath, contentType } : null;
  } catch {
    return null;
  }
}

async function assetFile(request: NextRequest): Promise<AssetFile | null> {
  const configuredRoot = process.env.SITE_ASSETS_DIR?.trim();
  const requested = requestedPath(request);
  if (!configuredRoot || !path.isAbsolute(configuredRoot) || !requested) return null;

  try {
    const root = await realpath(configuredRoot);
    const candidate = await realpath(path.resolve(root, requested.relativePath));
    if (!containedBy(root, candidate)) return null;
    if (MIME_TYPES[path.extname(candidate).toLowerCase()] !== requested.contentType) return null;

    const info = await stat(candidate);
    if (!info.isFile() || info.size > MAX_ASSET_SIZE_BYTES) return null;
    return {
      path: candidate,
      size: info.size,
      mtime: info.mtime,
      etag: `W/"${info.size.toString(16)}-${Math.trunc(info.mtimeMs).toString(16)}"`,
      contentType: requested.contentType,
    };
  } catch {
    return null;
  }
}

function isNotModified(request: NextRequest, asset: AssetFile): boolean {
  const ifNoneMatch = request.headers.get('if-none-match');
  if (ifNoneMatch) {
    const validators = ifNoneMatch.split(',').map((value) => value.trim());
    return validators.includes('*') || validators.includes(asset.etag);
  }

  const ifModifiedSince = request.headers.get('if-modified-since');
  if (!ifModifiedSince) return false;
  const since = Date.parse(ifModifiedSince);
  return Number.isFinite(since) && Math.floor(asset.mtime.getTime() / 1000) * 1000 <= since;
}

async function handle(request: NextRequest): Promise<Response> {
  const asset = await assetFile(request);
  if (!asset) return notFound();

  const headers = new Headers({
    'cache-control': CACHE_CONTROL,
    'content-length': String(asset.size),
    'content-type': asset.contentType,
    'cross-origin-resource-policy': 'same-origin',
    etag: asset.etag,
    'last-modified': asset.mtime.toUTCString(),
    'x-content-type-options': 'nosniff',
  });
  if (asset.contentType === 'image/svg+xml') {
    // SVG is an image format but can contain active content when navigated to
    // directly. Keep same-origin branding SVGs useful while sandboxing scripts
    // and all network loads from a compromised or accidentally unsafe asset.
    headers.set(
      'content-security-policy',
      "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    );
  }

  if (isNotModified(request, asset)) {
    headers.delete('content-length');
    return new NextResponse(null, { status: 304, headers });
  }

  if (request.method === 'HEAD') return new NextResponse(null, { status: 200, headers });

  const body = Readable.toWeb(createReadStream(asset.path)) as unknown as ReadableStream;
  return new NextResponse(body, { status: 200, headers });
}

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export { handle as GET, handle as HEAD };
