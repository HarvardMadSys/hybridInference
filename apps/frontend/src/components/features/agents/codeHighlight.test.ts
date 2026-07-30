import { describe, expect, it } from 'vitest';

import { highlightCode, type CodeToken } from './codeHighlight';

const text = (tokens: CodeToken[]): string => tokens.map((token) => token.text).join('');
const kinds = (tokens: CodeToken[], kind: CodeToken['kind']): string[] =>
  tokens.filter((token) => token.kind === kind).map((token) => token.text);

describe('highlightCode', () => {
  it('keeps every character of the source, line for line', () => {
    const source = 'const a = 1;\n\nif (a) {\n  return "x";\n}\n';
    const lines = highlightCode(source, 'app.ts');

    expect(lines.map(text).join('\n')).toBe(source);
  });

  it('classifies keywords, strings, numbers and comments', () => {
    const [line] = highlightCode('const total = 42; // sum', 'sum.ts');

    expect(kinds(line, 'keyword')).toEqual(['const']);
    expect(kinds(line, 'number')).toEqual(['42']);
    expect(kinds(line, 'comment')).toEqual(['// sum']);
  });

  it('carries multi-line strings and comments across line boundaries', () => {
    const lines = highlightCode('"""\ndoc\n"""\nx = 1\n', 'mod.py');

    expect(lines[0][0].kind).toBe('string');
    expect(lines[1][0]).toEqual({ text: 'doc', kind: 'string' });
    expect(kinds(lines[3], 'number')).toEqual(['1']);
  });

  it('does not run a single-quoted string past the end of its line', () => {
    const lines = highlightCode("label = 'unterminated\nkeep = 2\n", 'conf.py');

    expect(kinds(lines[0], 'string')).toEqual(["'unterminated"]);
    expect(kinds(lines[1], 'number')).toEqual(['2']);
  });

  it('colours markup tags and attributes', () => {
    const [line] = highlightCode('<p align="center">Hi</p>', 'README.md');

    expect(kinds(line, 'tag')).toEqual(['<p', '>', '</p', '>']);
    expect(kinds(line, 'attr')).toEqual(['align']);
    expect(kinds(line, 'string')).toEqual(['"center"']);
  });

  it('matches SQL keywords regardless of case and leaves unknown types plain', () => {
    const [sql] = highlightCode('SELECT id FROM users', 'query.sql');
    const [unknown] = highlightCode('SELECT id FROM users', 'notes.unknownext');

    expect(kinds(sql, 'keyword')).toEqual(['SELECT', 'FROM']);
    expect(unknown).toEqual([{ text: 'SELECT id FROM users', kind: 'plain' }]);
  });

  it('recognises extensionless well-known filenames', () => {
    const [line] = highlightCode('# base image\nFROM x', 'ops/Dockerfile');

    expect(kinds(line, 'comment')).toEqual(['# base image']);
  });
});
