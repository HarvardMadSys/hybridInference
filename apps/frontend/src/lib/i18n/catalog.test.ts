import { describe, expect, it } from 'vitest';

import {
  checkCatalog,
  collectCalls,
  sourcesBySlot,
} from '../../../scripts/i18n/check-catalogs.mjs';
import { catalogTranslator, type Catalog } from './translate';
import zhCN from './messages/zh-CN.json';

/**
 * The catalogs as shipped, checked against the source the way
 * `npm run i18n:check` does, so a malformed entry or a translation that drops a
 * `{placeholder}` fails the test suite rather than rendering a raw brace.
 */

describe('the shipped catalogs', () => {
  const { calls, errors } = collectCalls();
  const sources = sourcesBySlot(calls);

  it('reads every console string as a literal slot and literal English', () => {
    expect(errors).toEqual([]);
    expect(calls.length).toBeGreaterThan(0);
  });

  it('holds only well-formed entries whose placeholders match their source', () => {
    const report = checkCatalog(sources, zhCN);
    expect(report.errors).toEqual([]);
    expect(report.orphaned).toEqual([]);
    // Stale entries are allowed at runtime (they fall back to English), but a
    // change that makes one should re-translate it in the same change.
    expect(report.stale).toEqual([]);
  });

  it('translates the console chrome and the language controls', () => {
    const t = catalogTranslator(zhCN as Catalog);
    expect(t('chrome.header.dashboard', 'Dashboard')).toBe('控制台');
    expect(t('chrome.language.label', 'Interface language')).toBe('界面语言');
    expect(t('settings.language.title', 'Language')).toBe('语言');
  });
});

describe('catalogTranslator', () => {
  const catalog: Catalog = {
    'a.one': { source: 'Hello', text: '你好' },
    'a.two': [
      { source: 'All confirmations are required.', text: '需要全部确认。' },
      { source: 'All four confirmations are required.', text: '需要全部四项确认。' },
    ],
  };

  it('translates only the English the entry was made from', () => {
    const t = catalogTranslator(catalog);
    expect(t('a.one', 'Hello')).toBe('你好');
    // The English changed since: the old translation no longer applies.
    expect(t('a.one', 'Hello there')).toBe('Hello there');
    expect(t('a.missing', 'Untranslated')).toBe('Untranslated');
  });

  it('picks the entry for the English a multi-source slot passes', () => {
    const t = catalogTranslator(catalog);
    expect(t('a.two', 'All four confirmations are required.')).toBe('需要全部四项确认。');
    expect(t('a.two', 'All confirmations are required.')).toBe('需要全部确认。');
  });

  it("puts a module's wording first, including an intentionally empty one", () => {
    const t = catalogTranslator(catalog, { 'a.one': 'Module hello', 'a.two': '' });
    expect(t('a.one', 'Hello')).toBe('Module hello');
    expect(t('a.two', 'All confirmations are required.')).toBe('');
  });

  it('returns the English when there is nothing to translate with', () => {
    expect(catalogTranslator(undefined)('a.one', 'Hello')).toBe('Hello');
  });
});

describe('checkCatalog', () => {
  const sources = new Map([
    ['s.greet', new Set(['Hello {name}'])],
    ['s.two', new Set(['A', 'B'])],
  ]);

  it('reports each (slot, English) pair', () => {
    const report = checkCatalog(sources, {
      's.greet': { source: 'Hi {name}', text: '嗨 {name}' },
      's.two': { source: 'A', text: '甲' },
      's.gone': { source: 'Old', text: '旧' },
    });
    expect(report.errors).toEqual([]);
    expect(report.translated).toEqual([{ slot: 's.two', source: 'A' }]);
    expect(report.missing).toEqual([]);
    expect(report.stale).toEqual([
      { slot: 's.greet', source: 'Hello {name}' },
      { slot: 's.two', source: 'B' },
    ]);
    expect(report.orphaned).toEqual(['s.gone']);
  });

  it('refuses a translation that loses a placeholder, and a malformed entry', () => {
    const report = checkCatalog(sources, {
      's.greet': { source: 'Hello {name}', text: '你好' },
      's.two': { text: '甲' },
    });
    expect(report.errors).toHaveLength(2);
  });
});
