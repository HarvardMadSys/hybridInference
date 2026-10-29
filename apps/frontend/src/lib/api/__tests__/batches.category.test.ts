import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fetchWithAuth } from '../client';
import { BatchCategoryFilterUnavailable, listMyBatches } from '../batches';

vi.mock('../client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../client')>();
  return { ...actual, fetchWithAuth: vi.fn() };
});

const ROW = { id: 'batch_1', status: 'completed' };

function answer(body: Record<string, unknown>): Response {
  return new Response(JSON.stringify({ object: 'list', has_more: false, last_id: null, ...body }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

/** A gateway older than the categories: it ignores `category` and says nothing about it. */
const OLD = (data: unknown[] = [ROW]) => answer({ data, last_id: data.length ? 'batch_1' : null });
/** A current gateway: it says which category it applied. */
const NEW = (category: string | null, data: unknown[] = [ROW]) =>
  answer({ data, category_filter: true, category });

beforeEach(() => vi.mocked(fetchWithAuth).mockReset());

describe('a category-filtered list', () => {
  it('is accepted from a gateway that applied that category', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(NEW('issues'));
    const page = await listMyBatches(null, 20, { category: 'issues' });
    expect(page.data).toHaveLength(1);
    expect(vi.mocked(fetchWithAuth).mock.calls[0][1]).toContain('category=issues');
  });

  it('is refused from an older gateway that ignored the category, empty pages too', async () => {
    for (const old of [OLD(), OLD([])]) {
      vi.mocked(fetchWithAuth).mockResolvedValueOnce(old);
      await expect(listMyBatches(null, 20, { category: 'issues' })).rejects.toBeInstanceOf(
        BatchCategoryFilterUnavailable,
      );
    }
  });

  it('is refused when the gateway applied another category, or none', async () => {
    for (const other of [NEW('completed'), NEW(null)]) {
      vi.mocked(fetchWithAuth).mockResolvedValueOnce(other);
      await expect(listMyBatches(null, 20, { category: 'issues' })).rejects.toBeInstanceOf(
        BatchCategoryFilterUnavailable,
      );
    }
  });

  it('is checked on every page: a later page from an older gateway is refused', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(NEW('issues')).mockResolvedValueOnce(OLD());
    await listMyBatches(null, 20, { category: 'issues' });
    await expect(listMyBatches('batch_1', 20, { category: 'issues' })).rejects.toBeInstanceOf(
      BatchCategoryFilterUnavailable,
    );
  });
});

describe('an unfiltered list, or one by exact status', () => {
  it('works against an older gateway as before', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValueOnce(OLD()).mockResolvedValueOnce(OLD());
    expect((await listMyBatches(null, 20, {})).data).toHaveLength(1);
    expect((await listMyBatches(null, 20, { status: 'completed' })).data).toHaveLength(1);
    expect(vi.mocked(fetchWithAuth).mock.calls[1][1]).toContain('status=completed');
  });
});
