#!/usr/bin/env node
/**
 * Check the console's translation catalogs against the strings in the source.
 *
 * Every console string is a call `t('slot', 'English')` (or `translate(...)`):
 * the English at the call site is the source text, and a catalog
 * (`src/lib/i18n/messages/<locale>.json`) maps the slot to `{ source, text }`.
 * The runtime uses an entry only while its `source` is exactly the English the
 * call passes, so an edited sentence falls back to English until it is
 * translated again. This script reports that state and refuses the things that
 * would make it wrong:
 *
 *   errors (exit 1)   a call whose slot is a literal but whose English is not
 *                     built from literals; a catalog entry that is not
 *                     `{ source, text }`; a translation whose `{placeholders}`
 *                     differ from its source's
 *   reported          translated, missing (never translated), stale (the
 *                     English changed since), orphaned (no call uses the slot)
 *
 * A slot may pass different English in different places (`cond ? 'a' : 'b'`,
 * or two call sites); each English string is translated on its own, and the
 * catalog then holds a list of entries for that slot.
 *
 * A partial translation is allowed — missing and stale entries fall back to
 * English — so neither fails the check.
 *
 * Usage (from apps/frontend):
 *   node scripts/i18n/check-catalogs.mjs            check every catalog
 *   node scripts/i18n/check-catalogs.mjs --todo zh-CN
 *                                                   print the untranslated
 *                                                   (slot, English) pairs as JSON
 *   node scripts/i18n/check-catalogs.mjs --prune    drop orphaned entries
 */

import { createRequire } from 'node:module';
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const APP_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SRC_DIR = join(APP_DIR, 'src');
const CATALOG_DIR = join(SRC_DIR, 'lib', 'i18n', 'messages');

/** Callee names that resolve a slot. */
const TRANSLATORS = new Set(['t', 'translate']);
/** Slot keys look like `area.page.meaning`. */
const SLOT_PATTERN = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;

/** Skipped while walking the source: tests, and generated module bridges. */
function skip(path) {
  return (
    /\.test\.(ts|tsx)$/.test(path) ||
    path.includes(`${join('site-ui', 'active')}`) ||
    path.includes('node_modules')
  );
}

function sourceFiles(dir) {
  const files = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) files.push(...sourceFiles(path));
    else if (/\.(ts|tsx)$/.test(name) && !skip(path)) files.push(path);
  }
  return files;
}

/**
 * Every string an expression can be when it is built from literals — a literal,
 * `'a' + 'b'`, `cond ? 'a' : 'b'` — or `null` when it is not.
 */
function literalTexts(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [node.text];
  if (ts.isParenthesizedExpression(node)) return literalTexts(node.expression);
  if (ts.isConditionalExpression(node)) {
    const whenTrue = literalTexts(node.whenTrue);
    const whenFalse = literalTexts(node.whenFalse);
    return whenTrue === null || whenFalse === null ? null : [...whenTrue, ...whenFalse];
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = literalTexts(node.left);
    const right = literalTexts(node.right);
    if (left === null || right === null) return null;
    return left.flatMap((l) => right.map((r) => l + r));
  }
  return null;
}

/**
 * Every `t('slot', 'English')` call under `srcDir`, one record per English
 * string the call can pass.
 *
 * Returns the calls and the errors found reading them. A call whose first
 * argument is not a literal slot is not a console string (it is a helper
 * passing a slot through) and is ignored.
 */
export function collectCalls(srcDir = SRC_DIR) {
  const calls = [];
  const errors = [];
  for (const file of sourceFiles(srcDir)) {
    const text = readFileSync(file, 'utf8');
    const sourceFile = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
    const visit = (node) => {
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        TRANSLATORS.has(node.expression.text) &&
        node.arguments.length >= 2
      ) {
        const slots = literalTexts(node.arguments[0]);
        const slot = slots?.length === 1 ? slots[0] : null;
        if (slot !== null && SLOT_PATTERN.test(slot)) {
          const where = `${relative(APP_DIR, file)}:${
            sourceFile.getLineAndCharacterOfPosition(node.getStart()).line + 1
          }`;
          const sources = literalTexts(node.arguments[1]);
          if (sources === null) {
            errors.push(`${where}: the English for '${slot}' must be built from string literals`);
          } else {
            for (const source of sources) calls.push({ slot, source, where });
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return { calls, errors };
}

/** Each slot's English sources. A slot may say different English in different places. */
export function sourcesBySlot(calls) {
  const sources = new Map();
  for (const call of calls) {
    const set = sources.get(call.slot) ?? new Set();
    set.add(call.source);
    sources.set(call.slot, set);
  }
  return sources;
}

/** `{name}` placeholders in a string, sorted. */
function placeholders(text) {
  return [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
}

function isEntry(entry) {
  return (
    entry !== null &&
    typeof entry === 'object' &&
    typeof entry.source === 'string' &&
    typeof entry.text === 'string'
  );
}

/**
 * Compare one catalog with the calls, per (slot, English) pair.
 *
 * translated  the pair has an entry with that source
 * missing     the slot has no entry at all
 * stale       the slot has entries, none for this source — the English changed
 * orphaned    a catalog slot no call uses
 */
export function checkCatalog(sources, catalog) {
  const errors = [];
  const translated = [];
  const missing = [];
  const stale = [];
  const orphaned = [];

  for (const [slot, value] of Object.entries(catalog)) {
    const entries = Array.isArray(value) ? value : [value];
    if (entries.length === 0 || !entries.every(isEntry)) {
      errors.push(
        `'${slot}': a slot holds { "source": string, "text": string }, or a list of them`,
      );
      continue;
    }
    for (const entry of entries) {
      if (placeholders(entry.source).join() !== placeholders(entry.text).join()) {
        errors.push(
          `'${slot}': the translation of ${JSON.stringify(entry.source)} has placeholders ` +
            `[${placeholders(entry.text)}], the source [${placeholders(entry.source)}]`,
        );
      }
    }
    if (!sources.has(slot)) orphaned.push(slot);
  }

  for (const [slot, slotSources] of sources) {
    const value = catalog[slot];
    const entries = value === undefined ? [] : Array.isArray(value) ? value : [value];
    for (const source of slotSources) {
      const pair = { slot, source };
      if (entries.length === 0) missing.push(pair);
      else if (entries.some((entry) => entry.source === source)) translated.push(pair);
      else stale.push(pair);
    }
  }

  return { errors, translated, missing, stale, orphaned };
}

function catalogFiles() {
  return readdirSync(CATALOG_DIR)
    .filter((name) => name.endsWith('.json'))
    .map((name) => ({ locale: name.slice(0, -'.json'.length), path: join(CATALOG_DIR, name) }));
}

function writeCatalog(path, catalog) {
  const sorted = Object.fromEntries(
    Object.keys(catalog)
      .sort()
      .map((slot) => [slot, catalog[slot]]),
  );
  writeFileSync(path, `${JSON.stringify(sorted, null, 2)}\n`);
}

function main(argv) {
  const todoIndex = argv.indexOf('--todo');
  const todoLocale = todoIndex >= 0 ? argv[todoIndex + 1] : null;
  const prune = argv.includes('--prune');

  const { calls, errors: callErrors } = collectCalls();
  const sources = sourcesBySlot(calls);
  const pairs = [...sources.values()].reduce((n, set) => n + set.size, 0);
  let failed = callErrors.length > 0;
  for (const error of callErrors) console.error(`error: ${error}`);

  for (const { locale, path } of catalogFiles()) {
    const catalog = JSON.parse(readFileSync(path, 'utf8'));
    const report = checkCatalog(sources, catalog);

    if (todoLocale === locale) {
      // One line per untranslated pair; a slot with two sources appears twice.
      const todo = [...report.missing, ...report.stale].sort((a, b) =>
        a.slot.localeCompare(b.slot),
      );
      process.stdout.write(`${JSON.stringify(todo, null, 2)}\n`);
      continue;
    }

    for (const error of report.errors) console.error(`error: ${locale} ${error}`);
    failed ||= report.errors.length > 0;
    console.log(
      `${locale}: ${report.translated.length}/${pairs} translated, ` +
        `${report.missing.length} missing, ${report.stale.length} stale, ` +
        `${report.orphaned.length} orphaned`,
    );
    for (const { slot, source } of report.stale) {
      console.log(`  stale    ${slot}: now ${JSON.stringify(source)}`);
    }
    for (const slot of report.orphaned) console.log(`  orphaned ${slot}`);

    if (prune && report.orphaned.length > 0) {
      for (const slot of report.orphaned) delete catalog[slot];
      writeCatalog(path, catalog);
      console.log(`  pruned ${report.orphaned.length} orphaned slots`);
    }
  }

  if (todoLocale === null) console.log(`${calls.length} strings in ${sources.size} slots`);
  return failed ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
