import { describe, expect, it } from 'vitest';

import type { BatchStatus, ConsoleBatch } from '@/lib/api/batches';

import { batchCategory } from './batchText';

function old(
  status: BatchStatus,
  counts: { total: number; completed: number; failed: number },
  cancelled = 0,
): ConsoleBatch {
  // A batch as a gateway older than the categories sends it: no `category`.
  return { status, request_counts: counts, cancelled } as ConsoleBatch;
}

const OK = { total: 10, completed: 10, failed: 0 };

// The same cases as the server's rule (tests/integration/storage/test_batch_console_categories.py).
const CASES: [string, ConsoleBatch, string][] = [
  ['validating', old('validating', { total: 0, completed: 0, failed: 0 }), 'processing'],
  ['running', old('in_progress', OK), 'processing'],
  ['finalizing', old('finalizing', OK), 'processing'],
  ['cancelling', old('cancelling', { total: 10, completed: 3, failed: 0 }), 'processing'],
  ['all succeeded', old('completed', OK), 'completed'],
  ['partly failed', old('completed', { total: 1000, completed: 980, failed: 20 }), 'issues'],
  ['all failed', old('completed', { total: 10, completed: 0, failed: 10 }), 'issues'],
  ['failed', old('failed', { total: 0, completed: 0, failed: 0 }), 'issues'],
  ['expired', old('expired', { total: 10, completed: 4, failed: 6 }), 'issues'],
  ['cancelled, mixed', old('cancelled', { total: 10, completed: 5, failed: 2 }, 3), 'cancelled'],
];

describe('the category of a batch from an older gateway', () => {
  it.each(CASES)('%s', (_name, batch, expected) => {
    expect(batchCategory(batch)).toBe(expected);
  });

  it("defers to the gateway's own category when it sends one", () => {
    expect(batchCategory({ ...old('completed', OK), category: 'issues' })).toBe('issues');
  });
});
