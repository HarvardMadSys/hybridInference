/** Dependency-free syntax highlighting for the workspace file viewer.
 *
 * A real grammar engine would dwarf the rest of the frontend bundle, so this is
 * a single-pass scanner that classifies the things worth colouring: comments,
 * strings, numbers, keywords and markup tags. Anything it cannot classify stays
 * plain, which always renders correctly — the worst case is a bland preview.
 */

export type TokenKind = 'plain' | 'comment' | 'string' | 'number' | 'keyword' | 'tag' | 'attr';

export interface CodeToken {
  text: string;
  kind: TokenKind;
}

/** Tailwind text colours per token kind, tuned for the light workspace pane. */
export const TOKEN_CLASS: Record<TokenKind, string> = {
  plain: '',
  comment: 'text-gray-400',
  string: 'text-indigo-600',
  number: 'text-cyan-700',
  keyword: 'text-purple-700',
  tag: 'text-rose-700',
  attr: 'text-amber-700',
};

/** Past this size the scan is skipped so a huge preview cannot stall the tab. */
const MAX_HIGHLIGHT_CHARS = 200_000;

interface LanguageSpec {
  lineComments: readonly string[];
  blockComment?: readonly [string, string];
  quotes: readonly string[];
  /** Python-style `"""` / `'''` literals, which may span lines. */
  tripleQuotes?: boolean;
  keywords: ReadonlySet<string>;
  caseInsensitive?: boolean;
  /** Tag and attribute colouring for HTML/XML/Markdown. */
  markup?: boolean;
}

const words = (list: string): ReadonlySet<string> => new Set(list.split(' '));

const JS_KEYWORDS = words(
  'as async await break case catch class const continue debugger default delete do else enum export extends false finally for from function get if implements import in instanceof interface let new null of private protected public readonly return satisfies set static super switch this throw true try type typeof undefined var void while with yield',
);
const NATIVE_KEYWORDS = words(
  'bool break case char class const constexpr continue crate default defer do double else enum extern false final fn for func go if impl import int interface let long match mut namespace new nil null package private protected pub public range return self short static struct switch template this trait true try type typedef union unsafe use var virtual void where while',
);
const PY_KEYWORDS = words(
  'and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return self True try while with yield',
);
const SHELL_KEYWORDS = words(
  'case do done elif else esac export fi for function if in local return set source then unset until while',
);
const SQL_KEYWORDS = words(
  'add all alter and as asc between by case cast column create cross delete desc distinct drop else end exists from full group having in index inner insert into is join left limit not null on or order outer primary references right select set table then union unique update values view when where with',
);
const DATA_KEYWORDS = words('false no null off on true yes');

const SPECS = {
  js: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    quotes: ['"', "'", '`'],
    keywords: JS_KEYWORDS,
  },
  native: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    quotes: ['"', "'"],
    keywords: NATIVE_KEYWORDS,
  },
  py: {
    lineComments: ['#'],
    quotes: ['"', "'"],
    tripleQuotes: true,
    keywords: PY_KEYWORDS,
  },
  shell: { lineComments: ['#'], quotes: ['"', "'"], keywords: SHELL_KEYWORDS },
  sql: {
    lineComments: ['--'],
    blockComment: ['/*', '*/'],
    quotes: ["'", '"'],
    keywords: SQL_KEYWORDS,
    caseInsensitive: true,
  },
  json: { lineComments: [], quotes: ['"'], keywords: DATA_KEYWORDS },
  data: { lineComments: ['#'], quotes: ['"', "'"], keywords: DATA_KEYWORDS },
  css: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    quotes: ['"', "'"],
    keywords: words(''),
  },
  markup: {
    lineComments: [],
    blockComment: ['<!--', '-->'],
    quotes: ['"', "'"],
    keywords: words(''),
    markup: true,
  },
} as const satisfies Record<string, LanguageSpec>;

type SpecName = keyof typeof SPECS;

const EXTENSIONS: Record<string, SpecName> = {
  ts: 'js',
  tsx: 'js',
  mts: 'js',
  cts: 'js',
  js: 'js',
  jsx: 'js',
  mjs: 'js',
  cjs: 'js',
  py: 'py',
  pyi: 'py',
  rb: 'py',
  go: 'native',
  rs: 'native',
  c: 'native',
  h: 'native',
  cc: 'native',
  cpp: 'native',
  hpp: 'native',
  cs: 'native',
  java: 'native',
  kt: 'native',
  swift: 'native',
  php: 'native',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  fish: 'shell',
  sql: 'sql',
  json: 'json',
  jsonc: 'json',
  yaml: 'data',
  yml: 'data',
  toml: 'data',
  ini: 'data',
  cfg: 'data',
  conf: 'data',
  env: 'data',
  lock: 'data',
  properties: 'data',
  css: 'css',
  scss: 'css',
  less: 'css',
  html: 'markup',
  htm: 'markup',
  xml: 'markup',
  svg: 'markup',
  vue: 'markup',
  md: 'markup',
  markdown: 'markup',
  mdx: 'markup',
  rst: 'markup',
};

/** Files whose whole name — not extension — decides the language. */
const FILENAMES: Record<string, SpecName> = {
  dockerfile: 'shell',
  makefile: 'shell',
  procfile: 'shell',
  gemfile: 'py',
  '.env': 'data',
  '.gitignore': 'data',
  '.dockerignore': 'data',
  '.editorconfig': 'data',
};

function specForPath(path: string): LanguageSpec | null {
  const name = (path.split('/').pop() ?? '').toLowerCase();
  const byName = FILENAMES[name];
  if (byName) return SPECS[byName];
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return null;
  const byExtension = EXTENSIONS[name.slice(dot + 1)];
  return byExtension ? SPECS[byExtension] : null;
}

const isWordStart = (char: string): boolean => /[A-Za-z_$]/.test(char);
const isWordChar = (char: string): boolean => /[A-Za-z0-9_$]/.test(char);

/** Index just past the closing quote, or the line/document end if unterminated. */
function stringEnd(source: string, start: number, quote: string): number {
  let index = start + 1;
  while (index < source.length) {
    const char = source[index];
    if (char === '\\') {
      index += 2;
      continue;
    }
    if (char === quote) return index + 1;
    // Only template literals survive a newline; anything else is unterminated.
    if (char === '\n' && quote !== '`') return index;
    index += 1;
  }
  return source.length;
}

function scan(source: string, spec: LanguageSpec): CodeToken[] {
  const tokens: CodeToken[] = [];
  let plain = '';
  const flush = () => {
    if (plain) {
      tokens.push({ text: plain, kind: 'plain' });
      plain = '';
    }
  };
  const emit = (text: string, kind: TokenKind) => {
    flush();
    tokens.push({ text, kind });
  };

  let index = 0;
  let inTag = false;

  while (index < source.length) {
    const char = source[index];

    if (spec.blockComment && source.startsWith(spec.blockComment[0], index)) {
      const close = source.indexOf(spec.blockComment[1], index + spec.blockComment[0].length);
      const end = close === -1 ? source.length : close + spec.blockComment[1].length;
      emit(source.slice(index, end), 'comment');
      index = end;
      continue;
    }

    const lineComment = spec.lineComments.find((token) => source.startsWith(token, index));
    if (lineComment) {
      const newline = source.indexOf('\n', index);
      const end = newline === -1 ? source.length : newline;
      emit(source.slice(index, end), 'comment');
      index = end;
      continue;
    }

    if (spec.tripleQuotes && (source.startsWith('"""', index) || source.startsWith("'''", index))) {
      const fence = source.slice(index, index + 3);
      const close = source.indexOf(fence, index + 3);
      const end = close === -1 ? source.length : close + 3;
      emit(source.slice(index, end), 'string');
      index = end;
      continue;
    }

    if (spec.quotes.includes(char)) {
      const end = stringEnd(source, index, char);
      emit(source.slice(index, end), 'string');
      index = end;
      continue;
    }

    if (spec.markup) {
      if (char === '<') {
        const opening = /^<\/?[A-Za-z][\w:.-]*/.exec(source.slice(index, index + 64));
        if (opening) {
          emit(opening[0], 'tag');
          index += opening[0].length;
          inTag = true;
          continue;
        }
      }
      if (inTag && (char === '>' || source.startsWith('/>', index))) {
        const text = char === '>' ? '>' : '/>';
        emit(text, 'tag');
        index += text.length;
        inTag = false;
        continue;
      }
    }

    if (isWordStart(char)) {
      let end = index + 1;
      while (end < source.length && isWordChar(source[end])) end += 1;
      const word = source.slice(index, end);
      if (spec.markup && inTag) emit(word, 'attr');
      else if (spec.keywords.has(spec.caseInsensitive ? word.toLowerCase() : word))
        emit(word, 'keyword');
      else plain += word;
      index = end;
      continue;
    }

    if (char >= '0' && char <= '9') {
      let end = index + 1;
      while (end < source.length && /[\w.]/.test(source[end])) end += 1;
      emit(source.slice(index, end), 'number');
      index = end;
      continue;
    }

    plain += char;
    index += 1;
  }

  flush();
  return tokens;
}

/** Split tokens — some of which span newlines — into one array per line. */
function splitLines(tokens: CodeToken[]): CodeToken[][] {
  const lines: CodeToken[][] = [[]];
  for (const token of tokens) {
    const parts = token.text.split('\n');
    parts.forEach((part, index) => {
      if (index > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ text: part, kind: token.kind });
    });
  }
  return lines;
}

function plainLines(source: string): CodeToken[][] {
  return source.split('\n').map((line) => (line ? [{ text: line, kind: 'plain' as const }] : []));
}

/** Tokenize `source` for display, one token array per line of the file. */
export function highlightCode(source: string, path: string): CodeToken[][] {
  const spec = specForPath(path);
  if (!spec || source.length > MAX_HIGHLIGHT_CHARS) return plainLines(source);
  return splitLines(scan(source, spec));
}
