import { describe, expect, it } from 'vitest';

import {
  advancedKeys,
  duplicateModel,
  modelProblems,
  namedInRefusal,
  parseCatalog,
  parseNumberField,
  shortTokens,
  withAdvancedKeys,
  withExplicitRoutes,
  withKey,
} from './modelCatalog';

describe('modelCatalog', () => {
  it('reads a catalog only when it can write it back exactly', () => {
    expect(parseCatalog('')).toEqual({ document: {}, models: [] });
    expect(parseCatalog('{"models": [{"id": "a"}]}')?.models).toEqual([{ id: 'a' }]);
    expect(parseCatalog('[]')).toBeNull();
    expect(parseCatalog('{"models": {}}')).toBeNull();
    expect(parseCatalog('{"models": ["a"]}')).toBeNull();
    expect(parseCatalog('{"models": [{"id": "a", "n": 9007199254740993}]}')).toBeNull();
  });

  it('sets and removes keys in place', () => {
    expect(Object.keys(withKey({ a: 1, b: 2, c: 3 }, 'b', 9))).toEqual(['a', 'b', 'c']);
    expect(withKey({ a: 1, b: 2 }, 'b', undefined)).toEqual({ a: 1 });
    expect(withKey({ a: 1 }, 'z', true)).toEqual({ a: 1, z: true });
  });

  it('reads numbers, and says what is wrong with one', () => {
    expect(parseNumberField('', { integer: true, min: 1 })).toEqual({ value: undefined });
    expect(parseNumberField('8192', { integer: true, min: 1 })).toEqual({ value: 8192 });
    expect(parseNumberField('0.5', { integer: false, min: 0 })).toEqual({ value: 0.5 });
    expect(parseNumberField('1.5', { integer: true, min: 1 })).toHaveProperty('error');
    expect(parseNumberField('0', { integer: true, min: 1 })).toEqual({ error: 'Enter 1 or more.' });
  });

  it('splits the form keys from the rest and puts them back', () => {
    const model = { id: 'a', pricing: { input: '1' }, route: [], extra_body: {} };
    expect(advancedKeys(model)).toEqual({ pricing: { input: '1' }, extra_body: {} });
    expect(withAdvancedKeys(model, { processor: 'x' })).toEqual({
      id: 'a',
      route: [],
      processor: 'x',
    });
  });

  it('turns a shorthand model into one with routes', () => {
    expect(withExplicitRoutes({ id: 'a', provider: 'vllm', base_url: 'u', api_key: 'k' })).toEqual({
      id: 'a',
      provider: 'vllm',
      route: [{ weight: 1, base_url: 'u', api_key: 'k' }],
    });
  });

  it('copies a model under a free id, without its aliases', () => {
    expect(duplicateModel({ id: 'a', aliases: ['x'] }, ['a', 'a-copy'])).toEqual({
      id: 'a-copy-2',
    });
  });

  it('finds what the form can tell is wrong', () => {
    const problems = modelProblems([
      { id: 'a', provider: 'vllm', aliases: ['x'] },
      { id: 'a', provider: 'vllm' },
      { id: '', route: [{ base_url: 'u' }] },
      { id: 'c', provider: 'vllm', aliases: ['x'] },
    ]);
    expect(problems[0]).toBeUndefined();
    expect(problems[1]).toEqual(['Another model already has the id "a".']);
    expect(problems[2]).toEqual([
      'Give the model an id.',
      'Route 1 needs an adapter kind (or the model a provider).',
    ]);
    expect(problems[3]).toEqual(['The alias "x" is already a\'s.']);
  });

  it('shortens token counts and spots a refusal naming a model', () => {
    expect(shortTokens(8192)).toBe('8K');
    expect(shortTokens(131072)).toBe('128K');
    expect(shortTokens(1048576)).toBe('1M');
    expect(shortTokens(1000000)).toBe('1M');
    expect(shortTokens(200000)).toBe('200K');
    expect(namedInRefusal("bad (model 'a', route 1)", 'a')).toBe(true);
    expect(namedInRefusal("bad (model 'ab')", 'a')).toBe(false);
  });
});
