import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchWithAuth = vi.fn();

vi.mock('./client', () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuth(...args),
  jsonOrThrow: async (resp: { json: () => Promise<unknown> }) => resp.json(),
}));

import { getModels } from './user';

describe('getModels', () => {
  beforeEach(() => fetchWithAuth.mockReset());

  it('returns the account catalog in order of model id', async () => {
    fetchWithAuth.mockResolvedValue({
      json: async () => ({
        object: 'list',
        data: [{ id: 'qwen3-32b' }, { id: 'GLM-4.6' }, { id: 'qwen3-8b' }, { id: 'deepseek-v3' }],
      }),
    });

    const catalog = await getModels();

    expect(fetchWithAuth).toHaveBeenCalledWith(expect.anything(), '/user/models');
    expect(catalog.object).toBe('list');
    expect(catalog.data.map((model) => model.id)).toEqual([
      'deepseek-v3',
      'GLM-4.6',
      'qwen3-8b',
      'qwen3-32b',
    ]);
  });
});
