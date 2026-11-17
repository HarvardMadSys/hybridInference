import { describe, expect, it } from 'vitest';
import { compareModelIds, sortModelsById } from './modelOrder';

describe('sortModelsById', () => {
  it('orders models alphabetically by id, ignoring case', () => {
    const models = [{ id: 'qwen3-8b' }, { id: 'GLM-4.6' }, { id: 'deepseek-v3' }, { id: 'auto' }];

    expect(sortModelsById(models).map((m) => m.id)).toEqual([
      'auto',
      'deepseek-v3',
      'GLM-4.6',
      'qwen3-8b',
    ]);
  });

  it('compares runs of digits as numbers', () => {
    const models = [{ id: 'qwen3-32b' }, { id: 'qwen3-8b' }, { id: 'qwen3-235b' }];

    expect(sortModelsById(models).map((m) => m.id)).toEqual([
      'qwen3-8b',
      'qwen3-32b',
      'qwen3-235b',
    ]);
  });

  it('keeps the other fields and leaves the input untouched', () => {
    const models = [
      { id: 'b', name: 'B' },
      { id: 'a', name: 'A' },
    ];

    expect(sortModelsById(models)).toEqual([
      { id: 'a', name: 'A' },
      { id: 'b', name: 'B' },
    ]);
    expect(models.map((m) => m.id)).toEqual(['b', 'a']);
  });

  it('gives ids that differ only by case one fixed order', () => {
    expect(compareModelIds('Model', 'model')).toBe(-compareModelIds('model', 'Model'));
    expect(compareModelIds('Model', 'model')).not.toBe(0);
  });
});
