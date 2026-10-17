import { describe, expect, it } from 'vitest';
import { parseHiddenColumns } from './columns';

describe('parseHiddenColumns', () => {
  it('keeps known column ids', () => {
    expect(parseHiddenColumns('["tokens","automation"]')).toEqual(['tokens', 'automation']);
  });

  it('drops unknown ids and tolerates bad input', () => {
    expect(parseHiddenColumns('["tokens","gone",3]')).toEqual(['tokens']);
    expect(parseHiddenColumns('not json')).toEqual([]);
    expect(parseHiddenColumns('{"a":1}')).toEqual([]);
    expect(parseHiddenColumns(null)).toEqual([]);
  });
});
