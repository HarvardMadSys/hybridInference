'use client';

import { useEffect, useMemo, useState } from 'react';

import { writeAgentJobFile, type AgentJobFileEntryApi } from '@/lib/api/agents';

import { highlightCode, TOKEN_CLASS, type CodeToken } from './codeHighlight';
import type { AgentJob } from './types';
import { useWorkspaceFiles, type WorkspaceDirectory } from './useAgentJobs';

/** Rows rendered at once; the API already caps a preview at 512 KB. */
const MAX_PREVIEW_LINES = 4000;

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
      className={`h-3 w-3 shrink-0 text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

const GLYPHS: Record<string, { path: string; className: string }> = {
  code: { path: 'm9 8-4 4 4 4m6-8 4 4-4 4', className: 'text-sky-500' },
  data: {
    path: 'M9 5H7a2 2 0 0 0-2 2v3l-2 2 2 2v3a2 2 0 0 0 2 2h2m6-14h2a2 2 0 0 1 2 2v3l2 2-2 2v3a2 2 0 0 1-2 2h-2',
    className: 'text-amber-500',
  },
  doc: { path: 'M7 5h7l4 4v10H7V5Zm2 6h6m-6 4h6', className: 'text-gray-400' },
  shell: { path: 'm8 10 2 2-2 2m4 0h4M5 5h14v14H5V5Z', className: 'text-emerald-500' },
  media: { path: 'M5 6h14v12H5V6Zm2 8 3-3 3 3 2-2 2 2', className: 'text-violet-500' },
  plain: { path: 'M7 5h7l4 4v10H7V5Zm7 0v4h4', className: 'text-gray-300' },
};

const GLYPH_BY_EXTENSION: Record<string, keyof typeof GLYPHS> = {
  ts: 'code',
  tsx: 'code',
  js: 'code',
  jsx: 'code',
  py: 'code',
  go: 'code',
  rs: 'code',
  rb: 'code',
  java: 'code',
  c: 'code',
  h: 'code',
  cpp: 'code',
  css: 'code',
  scss: 'code',
  html: 'code',
  json: 'data',
  yaml: 'data',
  yml: 'data',
  toml: 'data',
  ini: 'data',
  cfg: 'data',
  env: 'data',
  lock: 'data',
  csv: 'data',
  sql: 'data',
  md: 'doc',
  markdown: 'doc',
  rst: 'doc',
  txt: 'doc',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  png: 'media',
  jpg: 'media',
  jpeg: 'media',
  gif: 'media',
  svg: 'media',
  webp: 'media',
  ico: 'media',
  pdf: 'media',
};

function FileGlyph({ name }: { name: string }) {
  const lower = name.toLowerCase();
  const dot = lower.lastIndexOf('.');
  const family =
    lower === 'dockerfile' || lower === 'makefile'
      ? 'shell'
      : ((dot > 0 ? GLYPH_BY_EXTENSION[lower.slice(dot + 1)] : undefined) ?? 'plain');
  const glyph = GLYPHS[family];
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      className={`h-3.5 w-3.5 shrink-0 ${glyph.className}`}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={glyph.path} />
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
  const indent = { paddingLeft: `${0.5 + depth * 0.75}rem` };

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
              style={indent}
              className={`flex w-full items-center gap-1.5 py-[3px] pr-2 text-left text-[12px] ${
                active
                  ? 'bg-gray-200/70 text-gray-900'
                  : 'text-gray-600 hover:bg-gray-200/40 hover:text-gray-900'
              }`}
            >
              {isDirectory ? <Caret open={open} /> : <FileGlyph name={entry.name} />}
              <span className="min-w-0 flex-1 truncate font-mono">{entry.name}</span>
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
          className={`flex w-full items-baseline gap-1.5 px-2 py-[3px] text-left text-[12px] ${
            selected === row.path
              ? 'bg-gray-200/70 text-gray-900'
              : 'text-gray-600 hover:bg-gray-200/40 hover:text-gray-900'
          }`}
        >
          <span className="min-w-0 flex-1 truncate font-mono">{basename(row.path)}</span>
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

/** Editable twin of CodeView: same gutter and metrics, no highlighting.
 *
 * The textarea grows to its content and never wraps, so the gutter stays
 * aligned line-for-line without any scroll syncing.
 */
function CodeEditor({
  path,
  draft,
  onChange,
}: {
  path: string;
  draft: string;
  onChange: (value: string) => void;
}) {
  const count = draft.split('\n').length;
  return (
    <div className="flex py-2 font-mono text-[12px] leading-[1.7]">
      <div aria-hidden="true" className="w-12 shrink-0 select-none pr-4 text-right text-gray-300">
        {Array.from({ length: count }, (_, index) => (
          <div key={index}>{index + 1}</div>
        ))}
      </div>
      <textarea
        value={draft}
        onChange={(event) => onChange(event.target.value)}
        aria-label={`Edit ${path}`}
        spellCheck={false}
        autoFocus
        wrap="off"
        rows={count + 1}
        className="min-w-0 flex-1 resize-none border-0 bg-white p-0 pr-4 font-mono text-[12px] leading-[1.7] text-gray-700 outline-none"
      />
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
 * Live worktrees are editable, archived snapshots are read-only — the header
 * says which, so an unexpectedly missing Save button has a visible reason.
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
  const [narrow, setNarrow] = useState(false);
  const [mode, setMode] = useState<SidebarMode>('files');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const root = directories[''];
  const editable = file?.kind === 'file' && Boolean(file.writable) && file.status !== 'deleted';
  // Below sm there is no room for both panes, so the tree and the file take
  // turns and the header button walks back to the tree.
  const showTree = sidebarOpen && !(narrow && selected);

  useEffect(() => {
    const query = window.matchMedia?.('(max-width: 639px)');
    if (!query) return undefined;
    setNarrow(query.matches);
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  useEffect(() => {
    setEditing(false);
    setSaveError(null);
    setDraft(file?.kind === 'file' ? (file.content ?? '') : '');
  }, [file]);

  const save = async () => {
    if (file?.kind !== 'file' || !editable || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await writeAgentJobFile(job.id, file.path, draft);
      setEditing(false);
      reload();
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : 'Could not save the file');
    } finally {
      setSaving(false);
    }
  };

  const sourceLabel = root?.error
    ? 'Unavailable'
    : (file?.source ?? source) === 'workspace'
      ? 'Live worktree'
      : root?.entries || file
        ? 'Archived snapshot'
        : 'Loading…';

  return (
    <section
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
            <span className="truncate font-mono text-[13px] text-gray-800">
              {basename(selected)}
            </span>
            {dirname(selected) ? (
              <span className="hidden truncate font-mono text-[11px] text-gray-400 sm:inline">
                {dirname(selected)}
              </span>
            ) : null}
          </span>
        ) : (
          <span className="font-mono text-[13px] text-gray-500">Files</span>
        )}

        <span className="ml-auto shrink-0 rounded bg-white px-2 py-0.5 text-[11px] text-gray-400 ring-1 ring-gray-200">
          {sourceLabel}
        </span>

        {editable ? (
          editing ? (
            <>
              <button
                type="button"
                onClick={() => {
                  setEditing(false);
                  setDraft(file?.kind === 'file' ? (file.content ?? '') : '');
                  setSaveError(null);
                }}
                className="shrink-0 rounded-md px-2 py-1 text-[11px] text-gray-500 hover:bg-gray-200/60 hover:text-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={save}
                disabled={saving || draft === (file.kind === 'file' ? (file.content ?? '') : '')}
                className="shrink-0 rounded-md bg-gray-900 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-gray-800 disabled:bg-gray-200 disabled:text-gray-400"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="shrink-0 rounded-md border border-gray-200 bg-white px-2.5 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-50 hover:text-gray-900"
            >
              Edit
            </button>
          )
        ) : null}
      </div>

      <div className="flex min-h-0 flex-1">
        {showTree ? (
          <aside className="flex w-full shrink-0 flex-col border-r border-gray-200 bg-gray-50/40 sm:w-56">
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
            <div className="min-h-0 flex-1 overflow-auto py-1">
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
          className={`min-w-0 flex-1 overflow-auto bg-white ${showTree ? 'hidden sm:block' : ''}`}
        >
          {saveError ? (
            <p
              role="alert"
              className="border-b border-red-100 bg-red-50 px-4 py-2 text-xs text-red-600"
            >
              {saveError}
            </p>
          ) : null}

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
              {editing ? (
                <CodeEditor path={file.path} draft={draft} onChange={setDraft} />
              ) : (
                <div
                  onDoubleClick={() => {
                    if (editable) setEditing(true);
                  }}
                >
                  <CodeView path={file.path} content={file.content ?? ''} />
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
