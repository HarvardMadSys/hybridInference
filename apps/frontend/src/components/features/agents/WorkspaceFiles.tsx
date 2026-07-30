'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { AgentJobFileEntryApi } from '@/lib/api/agents';

import { highlightCode, TOKEN_CLASS, type CodeToken } from './codeHighlight';
import type { AgentJob } from './types';
import { useWorkspaceFiles, type WorkspaceDirectory } from './useAgentJobs';
import { PANE_RESIZE_EVENT } from './useResizablePane';

/** Rows rendered at once; the API already caps a preview at 512 KB. */
const MAX_PREVIEW_LINES = 4000;

/** Below this the tree and the file take turns instead of sharing the width. */
const TWO_PANE_MIN_WIDTH = 560;

type SidebarMode = 'files' | 'changes';

type FileStatus = 'added' | 'modified' | 'deleted';

function statusClass(status?: FileStatus | null): string {
  if (status === 'added') return 'text-emerald-600';
  if (status === 'deleted') return 'text-red-500';
  return 'text-amber-600';
}

function formatFileSize(size?: number | null): string {
  if (size === undefined || size === null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function basename(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path;
}

function dirname(path: string): string {
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

/** Directories first, then files, each alphabetical — the familiar tree order. */
function sortEntries(entries: AgentJobFileEntryApi[]): AgentJobFileEntryApi[] {
  return [...entries].sort((left, right) => {
    const leftDir = left.kind === 'directory' ? 0 : 1;
    const rightDir = right.kind === 'directory' ? 0 : 1;
    if (leftDir !== rightDir) return leftDir - rightDir;
    return left.name.localeCompare(right.name);
  });
}

function Caret({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.4}
      className={`h-3 w-3 shrink-0 text-gray-500 transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

// One glyph per kind of file, the way an editor sidebar names things: a
// changelog is a clock, a lock file is a padlock. Monochrome on purpose —
// colour in this pane is reserved for the added/modified/deleted marks.
const GLYPHS = {
  info: { paths: ['M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z', 'M12 10.5v6'], dot: [12, 8] },
  clock: { paths: ['M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z', 'M12 8v4.5l3 2'] },
  key: {
    paths: [
      'M12 4.2a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4Z',
      'M12 10.6V19.5',
      'M12 14h2.6',
      'M12 16.8h2',
    ],
  },
  gear: {
    paths: [
      'M12 8.6a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8Z',
      'M12 4v2.2M12 17.8V20M5.1 8l1.9 1.1M17 14.9l1.9 1.1M5.1 16l1.9-1.1M17 9.1 18.9 8',
    ],
  },
  lock: { paths: ['M6.5 11h11v9h-11v-9Z', 'M9 11V8a3 3 0 0 1 6 0v3'] },
  sliders: { paths: ['M4.5 7.5h15', 'M4.5 12h10', 'M4.5 16.5h6'] },
  diamond: { paths: ['M12 3.2 20.8 12 12 20.8 3.2 12 12 3.2Z'] },
  code: { paths: ['m9 8-4 4 4 4', 'm15 8 4 4-4 4'] },
  braces: {
    paths: [
      'M9.5 4.5H8a2 2 0 0 0-2 2v3l-2 2.5 2 2.5v3a2 2 0 0 0 2 2h1.5',
      'M14.5 4.5H16a2 2 0 0 1 2 2v3l2 2.5-2 2.5v3a2 2 0 0 1-2 2h-1.5',
    ],
  },
  doc: { paths: ['M7 4.5h6.5L18 9v10.5H7V4.5Z', 'M13 4.5V9h5', 'M9.5 13h5', 'M9.5 16h5'] },
  terminal: { paths: ['M4.5 5h15v14h-15V5Z', 'm8 10 2 2-2 2', 'M12.5 14h4'] },
  image: { paths: ['M4.5 6h15v12h-15V6Z', 'm7 14.5 3-3 2.5 2.5 2-2 2.5 2.5'] },
  table: { paths: ['M4.5 6h15v12h-15V6Z', 'M4.5 10.5h15', 'M10 6v12'] },
  plain: { paths: ['M7 4.5h6.5L18 9v10.5H7V4.5Z', 'M13 4.5V9h5'] },
} as const;

type GlyphName = keyof typeof GLYPHS;

/** Whole-name matches, checked as prefixes so `README.zh.md` still counts. */
const GLYPH_BY_NAME: Array<[string, GlyphName]> = [
  ['readme', 'info'],
  ['changelog', 'clock'],
  ['history', 'clock'],
  ['license', 'key'],
  ['licence', 'key'],
  ['copying', 'key'],
  ['notice', 'key'],
  ['.env', 'sliders'],
  ['.gitignore', 'diamond'],
  ['.gitattributes', 'diamond'],
  ['.dockerignore', 'diamond'],
  ['.editorconfig', 'sliders'],
  ['dockerfile', 'terminal'],
  ['makefile', 'terminal'],
];

const GLYPH_BY_EXTENSION: Record<string, GlyphName> = {
  ts: 'code',
  tsx: 'code',
  js: 'code',
  jsx: 'code',
  mjs: 'code',
  cjs: 'code',
  py: 'code',
  go: 'code',
  rs: 'code',
  rb: 'code',
  java: 'code',
  kt: 'code',
  swift: 'code',
  c: 'code',
  h: 'code',
  cc: 'code',
  cpp: 'code',
  cs: 'code',
  php: 'code',
  css: 'code',
  scss: 'code',
  html: 'code',
  vue: 'code',
  json: 'braces',
  jsonc: 'braces',
  yaml: 'braces',
  yml: 'braces',
  toml: 'gear',
  ini: 'gear',
  cfg: 'gear',
  conf: 'gear',
  properties: 'gear',
  lock: 'lock',
  pem: 'lock',
  key: 'lock',
  crt: 'lock',
  md: 'doc',
  markdown: 'doc',
  mdx: 'doc',
  rst: 'doc',
  txt: 'doc',
  log: 'doc',
  sh: 'terminal',
  bash: 'terminal',
  zsh: 'terminal',
  fish: 'terminal',
  csv: 'table',
  tsv: 'table',
  parquet: 'table',
  sql: 'table',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  svg: 'image',
  webp: 'image',
  ico: 'image',
  pdf: 'image',
};

function glyphFor(name: string): GlyphName {
  const lower = name.toLowerCase();
  const byName = GLYPH_BY_NAME.find(([prefix]) => lower.startsWith(prefix));
  if (byName) return byName[1];
  const dot = lower.lastIndexOf('.');
  return (dot > 0 ? GLYPH_BY_EXTENSION[lower.slice(dot + 1)] : undefined) ?? 'plain';
}

function FileGlyph({ name }: { name: string }) {
  const glyph = GLYPHS[glyphFor(name)];
  const dot = 'dot' in glyph ? glyph.dot : undefined;
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0 text-gray-500"
    >
      {glyph.paths.map((d) => (
        <path key={d} d={d} />
      ))}
      {dot ? <circle cx={dot[0]} cy={dot[1]} r={0.9} fill="currentColor" stroke="none" /> : null}
    </svg>
  );
}

function StatusMark({ status }: { status?: FileStatus | null }) {
  if (!status) return null;
  return (
    <span
      title={status}
      className={`shrink-0 text-[10px] font-semibold uppercase ${statusClass(status)}`}
    >
      {status.charAt(0)}
    </span>
  );
}

function TreeLevel({
  path,
  depth,
  directories,
  expanded,
  selected,
  onToggle,
  onOpen,
}: {
  path: string;
  depth: number;
  directories: Record<string, WorkspaceDirectory>;
  expanded: Record<string, boolean>;
  selected: string | null;
  onToggle: (path: string) => void;
  onOpen: (path: string) => void;
}) {
  const state = directories[path];
  const indent = { paddingLeft: `${0.5 + depth * 0.875}rem` };

  if (state?.error) {
    return (
      <p role="alert" style={indent} className="py-1 pr-2 text-[11px] text-red-600">
        {state.error}
      </p>
    );
  }
  if (!state?.entries) {
    return state?.loading ? (
      <p style={indent} className="py-1 pr-2 text-[11px] text-gray-400">
        Loading…
      </p>
    ) : null;
  }
  if (state.entries.length === 0) {
    return (
      <p style={indent} className="py-1 pr-2 text-[11px] text-gray-400">
        Empty
      </p>
    );
  }

  return (
    <>
      {sortEntries(state.entries).map((entry) => {
        const isDirectory = entry.kind === 'directory';
        const open = Boolean(expanded[entry.path]);
        const active = selected === entry.path;
        return (
          <div key={entry.path}>
            <button
              type="button"
              onClick={() => (isDirectory ? onToggle(entry.path) : onOpen(entry.path))}
              aria-expanded={isDirectory ? open : undefined}
              title={entry.name}
              style={indent}
              className={`flex w-full items-center gap-2 rounded-md py-[5px] pr-2 text-left text-[13px] ${
                active ? 'bg-gray-200/60 text-gray-900' : 'text-gray-800 hover:bg-gray-100'
              }`}
            >
              {isDirectory ? <Caret open={open} /> : <FileGlyph name={entry.name} />}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              <StatusMark status={entry.status ?? null} />
            </button>
            {isDirectory && open ? (
              <TreeLevel
                path={entry.path}
                depth={depth + 1}
                directories={directories}
                expanded={expanded}
                selected={selected}
                onToggle={onToggle}
                onOpen={onOpen}
              />
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function ChangedList({
  job,
  selected,
  onOpen,
}: {
  job: AgentJob;
  selected: string | null;
  onOpen: (path: string) => void;
}) {
  const details = job.diffFileDetails ?? [];
  const rows = job.diffFiles.map((path) => ({
    path,
    stat: details.find((file) => file.path === path),
  }));

  if (rows.length === 0) {
    return <p className="px-3 py-4 text-[11px] text-gray-400">This run changed no files.</p>;
  }

  return (
    <>
      {rows.map((row) => (
        <button
          key={row.path}
          type="button"
          onClick={() => onOpen(row.path)}
          title={row.path}
          className={`flex w-full items-center gap-2 rounded-md py-[5px] pl-2 pr-2 text-left text-[13px] ${
            selected === row.path
              ? 'bg-gray-200/60 text-gray-900'
              : 'text-gray-800 hover:bg-gray-100'
          }`}
        >
          <FileGlyph name={basename(row.path)} />
          <span className="min-w-0 flex-1 truncate">{basename(row.path)}</span>
          {row.stat ? (
            <span className="shrink-0 font-mono text-[10px] text-gray-400">
              <span className="text-emerald-600">+{row.stat.add}</span>{' '}
              <span className="text-red-500">−{row.stat.del}</span>
            </span>
          ) : null}
        </button>
      ))}
    </>
  );
}

/** One line of tokens; plain runs stay bare text so lines read as one string. */
function TokenLine({ tokens }: { tokens: CodeToken[] }) {
  return (
    <>
      {tokens.map((token, index) =>
        token.kind === 'plain' ? (
          token.text
        ) : (
          <span key={index} className={TOKEN_CLASS[token.kind]}>
            {token.text}
          </span>
        ),
      )}
    </>
  );
}

function CodeView({ path, content }: { path: string; content: string }) {
  const lines = useMemo(
    () =>
      highlightCode(content, path).map((tokens) => ({
        tokens,
        // A token too long to fit (a URL, a hash) has to break mid-token, or
        // the browser breaks at the indent instead and leaves the row visibly
        // empty. Ordinary lines keep wrapping on word boundaries.
        breakAnywhere: /\S{60,}/.test(tokens.map((token) => token.text).join('')),
      })),
    [content, path],
  );
  const shown = lines.slice(0, MAX_PREVIEW_LINES);

  return (
    <div className="py-2 font-mono text-[12px] leading-[1.7]">
      {shown.map(({ tokens, breakAnywhere }, index) => (
        // The line number *is* the identity here, so the index is a valid key.
        <div key={index} className="flex hover:bg-gray-50">
          <span
            aria-hidden="true"
            className="w-12 shrink-0 select-none pr-4 text-right text-gray-300"
          >
            {index + 1}
          </span>
          {/* Wrapped continuations hang under the line, as an editor would. */}
          <span
            className={`min-w-0 flex-1 whitespace-pre-wrap pl-6 pr-4 -indent-6 text-gray-700 ${
              breakAnywhere ? 'break-all' : 'break-words'
            }`}
          >
            {tokens.length ? <TokenLine tokens={tokens} /> : ' '}
          </span>
        </div>
      ))}
      {lines.length > shown.length ? (
        <p className="px-4 py-3 text-[11px] text-gray-400">
          Preview limited to the first {MAX_PREVIEW_LINES.toLocaleString()} lines.
        </p>
      ) : null}
    </div>
  );
}

function PaneMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center px-6 py-16 text-center text-sm text-gray-400">
      {children}
    </div>
  );
}

/**
 * Two-pane workspace browser: a lazily expanded tree beside a file viewer.
 *
 * Read-only: the workspace write endpoint exists, but changing a run's files
 * by hand belongs in the conversation, not in this pane. The header still says
 * whether this is the live worktree or an archived snapshot, because that
 * decides whether what you are reading can still change.
 */
export function WorkspaceFiles({ job, active }: { job: AgentJob; active: boolean }) {
  const {
    directories,
    expanded,
    selected,
    file,
    fileLoading,
    fileError,
    source,
    toggleDirectory,
    openFile,
    closeFile,
    reload,
  } = useWorkspaceFiles(job.id, active, `${job.state}:${job.diffFiles.length}`);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [paneWidth, setPaneWidth] = useState(0);
  const [mode, setMode] = useState<SidebarMode>('files');
  const paneRef = useRef<HTMLElement | null>(null);

  const root = directories[''];
  // The workspace pane is user-resizable, so whether there is room for two
  // panes is a fact about this box and not about the viewport. Unmeasured (SSR,
  // jsdom) counts as roomy: the two-pane layout is the normal one.
  const narrow = paneWidth > 0 && paneWidth < TWO_PANE_MIN_WIDTH;
  // When it is not, the tree and the file take turns and the header button
  // walks back to the tree.
  const showTree = sidebarOpen && !(narrow && selected);

  const attachPane = useCallback((node: HTMLElement | null) => {
    paneRef.current = node;
    if (node) setPaneWidth(node.clientWidth);
  }, []);

  useEffect(() => {
    const measure = () => {
      if (paneRef.current) setPaneWidth(paneRef.current.clientWidth);
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener(PANE_RESIZE_EVENT, measure);
    const observer =
      typeof ResizeObserver === 'undefined' || !paneRef.current
        ? null
        : new ResizeObserver(measure);
    observer?.observe(paneRef.current as HTMLElement);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener(PANE_RESIZE_EVENT, measure);
      observer?.disconnect();
    };
  }, []);

  const sourceLabel = root?.error
    ? 'Unavailable'
    : (file?.source ?? source) === 'workspace'
      ? 'Live worktree'
      : root?.entries || file
        ? 'Archived snapshot'
        : 'Loading…';

  return (
    <section
      ref={attachPane}
      aria-label="Workspace files"
      className="flex h-[36rem] max-h-[calc(100vh-11rem)] flex-col overflow-hidden rounded-xl border border-gray-200 bg-white"
    >
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-gray-200 bg-gray-50/70 px-2.5">
        <button
          type="button"
          onClick={() => (narrow && selected ? closeFile() : setSidebarOpen((open) => !open))}
          aria-label={showTree ? 'Hide file tree' : 'Show file tree'}
          aria-pressed={showTree}
          className={`shrink-0 rounded-md p-1.5 ${
            showTree
              ? 'bg-gray-200/70 text-gray-700'
              : 'text-gray-400 hover:bg-gray-200/50 hover:text-gray-700'
          }`}
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            className="h-4 w-4"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>

        {selected ? (
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-[13px] font-medium text-gray-900">
              {basename(selected)}
            </span>
            {dirname(selected) && !narrow ? (
              <span className="truncate text-[11px] text-gray-400">{dirname(selected)}</span>
            ) : null}
          </span>
        ) : (
          <span className="text-[13px] text-gray-500">Files</span>
        )}

        <span className="ml-auto shrink-0 rounded bg-white px-2 py-0.5 text-[11px] text-gray-400 ring-1 ring-gray-200">
          {sourceLabel}
        </span>
      </div>

      <div className="flex min-h-0 flex-1">
        {showTree ? (
          <aside
            className={`flex shrink-0 flex-col border-r border-gray-200 bg-white ${
              narrow ? 'w-full' : 'w-56'
            }`}
          >
            <div
              role="tablist"
              aria-label="File list"
              className="flex shrink-0 items-center gap-3 border-b border-gray-200 px-3 py-2 text-[12px]"
            >
              {(
                [
                  ['files', 'Files'],
                  ['changes', 'Changes'],
                ] as Array<[SidebarMode, string]>
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={mode === key}
                  onClick={() => setMode(key)}
                  className={
                    mode === key ? 'font-medium text-gray-900' : 'text-gray-400 hover:text-gray-700'
                  }
                >
                  {label}
                  {key === 'changes' && job.diffFiles.length ? (
                    <span className="ml-1 text-[10px] text-gray-400">{job.diffFiles.length}</span>
                  ) : null}
                </button>
              ))}
              <button
                type="button"
                onClick={reload}
                aria-label="Refresh files"
                className="ml-auto rounded p-1 text-gray-400 hover:bg-gray-200/60 hover:text-gray-700"
              >
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.8}
                  className="h-3.5 w-3.5"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M4 12a8 8 0 0 1 13.7-5.6L20 8m0-4v4h-4M20 12a8 8 0 0 1-13.7 5.6L4 16m0 4v-4h4"
                  />
                </svg>
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto px-1 py-1">
              {mode === 'files' ? (
                <TreeLevel
                  path=""
                  depth={0}
                  directories={directories}
                  expanded={expanded}
                  selected={selected}
                  onToggle={toggleDirectory}
                  onOpen={openFile}
                />
              ) : (
                <ChangedList job={job} selected={selected} onOpen={openFile} />
              )}
            </div>
          </aside>
        ) : null}

        <div
          className={`min-w-0 flex-1 overflow-auto bg-white ${showTree && narrow ? 'hidden' : ''}`}
        >
          {fileLoading ? (
            <PaneMessage>Loading file…</PaneMessage>
          ) : fileError ? (
            <PaneMessage>
              <span role="alert" className="text-red-600">
                {fileError}
              </span>
            </PaneMessage>
          ) : !selected || !file ? (
            <PaneMessage>
              {root?.error ? (
                <span className="flex flex-col items-center gap-3">
                  <span role="alert" className="text-red-600">
                    {root.error}
                  </span>
                  <button
                    type="button"
                    onClick={reload}
                    className="rounded-md border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
                  >
                    Try again
                  </button>
                </span>
              ) : (
                'Select a file to preview it here.'
              )}
            </PaneMessage>
          ) : file.kind === 'symlink' ? (
            <PaneMessage>Symlinks are not previewed.</PaneMessage>
          ) : file.status === 'deleted' ? (
            <PaneMessage>This file was deleted by the run.</PaneMessage>
          ) : file.binary ? (
            <PaneMessage>Binary files cannot be previewed.</PaneMessage>
          ) : (
            <>
              <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-1.5 text-[11px] text-gray-400">
                <span>{formatFileSize(file.size)}</span>
                {file.status ? (
                  <span className={`font-medium ${statusClass(file.status)}`}>{file.status}</span>
                ) : null}
                {file.truncated ? <span>Preview truncated</span> : null}
              </div>
              <CodeView path={file.path} content={file.content ?? ''} />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
