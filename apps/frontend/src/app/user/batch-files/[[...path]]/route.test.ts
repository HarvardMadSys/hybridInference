/**
 * Batch uploads reach the gateway through a route handler, which streams them,
 * and not through the `/user/:path*` rewrite, which would copy them into memory
 * and cut them off at `middlewareClientMaxBodySize`.
 */
import { describe, expect, it } from 'vitest';

import { proxyToGateway } from '@/lib/server/gatewayProxy';

import * as route from './route';

describe('/user/batch-files', () => {
  it('relays every method through the streaming gateway proxy', () => {
    for (const method of ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'] as const) {
      expect(route[method], method).toBe(proxyToGateway);
    }
    expect(route.runtime).toBe('nodejs');
  });
});
