'use client';

import {
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
} from 'react';
import { useRouter } from 'next/navigation';
import toast from 'react-hot-toast';

import { Markdown } from '@/components/ui/Markdown';
import {
  cancelAgentJob,
  followUpAgentJob,
  forkAgentJob,
  getAgentJobGit,
  type AgentGitWorkspaceApi,
} from '@/lib/api/agents';

import { lifecyclePhaseLabel, toDiffFileDetails } from './adapt';
import { PaneResizer } from './PaneResizer';
import { TerminalWorkspace } from './TerminalWorkspace';
import type { AgentEvent, AgentJob, AgentThreadMessage } from './types';
import { useResizablePane } from './useResizablePane';
import { WorkspaceFiles } from './WorkspaceFiles';

type DrawerView = 'overview' | 'diff' | 'raw';
type WorkspaceTab = 'git' | 'terminal' | 'files';

export const WORKSPACE_WIDTH_STORAGE_KEY = 'agents.workspace.width';
const WORKSPACE_DEFAULT_WIDTH = 560;
const WORKSPACE_MIN_WIDTH = 360;
const WORKSPACE_MAX_WIDTH = 1200;
// The transcript is the reason the page exists; the workspace stops widening
// before the conversation and its composer get squeezed into a column.
const TRANSCRIPT_MIN_WIDTH = 400;

const WORKSPACE_TABS: Array<{ key: WorkspaceTab; label: string }> = [
  { key: 'git', label: 'Git' },
  { key: 'terminal', label: 'Terminal' },
  { key: 'files', label: 'Files' },
];

const STATE_PILL: Record<AgentJob['state'], { label: string; className: string; dot?: string }> = {
  running: { label: 'Running', className: 'bg-blue-50 text-blue-700', dot: 'bg-blue-500' },
  queued: { label: 'Queued', className: 'bg-gray-100 text-gray-600', dot: 'bg-gray-400' },
  needs_review: { label: 'Needs review', className: 'bg-amber-50 text-amber-700' },
  done: { label: 'Done', className: 'bg-emerald-50 text-emerald-700' },
  failed: { label: 'Failed', className: 'bg-red-50 text-red-600' },
  cancelled: { label: 'Cancelled', className: 'bg-gray-100 text-gray-500' },
};

const DIFF_LINE_CLASS = {
  hunk: 'bg-gray-50 py-1 text-gray-400',
  ctx: 'text-gray-600',
  add: 'bg-emerald-50 text-emerald-700',
  del: 'bg-red-50 text-red-700',
} as const;

function Chevron({ className = '' }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={`h-4 w-4 ${className}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

function ToolIcon({ tool }: { tool: string }) {
  const normalized = tool.toLowerCase();
  let path = 'M12 4v16m8-8H4';
  if (normalized.includes('read') || normalized.includes('search')) {
    path =
      'M12 6.25c-2.5-2-6.5-2-9 0v12c2.5-2 6.5-2 9 0 2.5-2 6.5-2 9 0v-12c-2.5-2-6.5-2-9 0Zm0 0v12';
  } else if (normalized.includes('bash') || normalized.includes('command')) {
    path =
      'm7 8 4 4-4 4m6 0h4M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z';
  } else if (normalized.includes('edit') || normalized.includes('write')) {
    path =
      'M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z';
  }
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4 shrink-0 text-gray-400"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={path} />
    </svg>
  );
}

function LifecycleRow({ text }: { text: string }) {
  const { text: label, milestone } = lifecyclePhaseLabel(text);

  // A phase this build does not recognise is a diagnostic, not an achievement:
  // no tick, muted, and clearly a passthrough of what the runtime said.
  if (!milestone) {
    return (
      <div className="flex items-center gap-2.5 py-1 text-xs text-gray-400">
        <span className="h-1 w-1 rounded-full bg-gray-300" />
        <span className="font-mono">{label}</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2.5 py-2 text-[13px] text-gray-500">
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-gray-100 text-gray-500">
        <svg
          aria-hidden="true"
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 12 4 4 8-9" />
        </svg>
      </span>
      <span className="capitalize">{label}</span>
    </div>
  );
}

function ToolActivity({ event }: { event: Extract<AgentEvent, { kind: 'tool_use' }> }) {
  return (
    <details className="group my-1 rounded-lg border border-gray-200 bg-white">
      <summary className="flex cursor-pointer list-none items-center gap-2.5 px-3 py-2 text-[13px] text-gray-700 marker:content-none">
        <Chevron className="shrink-0 text-gray-400 transition-transform group-open:rotate-90" />
        <ToolIcon tool={event.tool} />
        <span className="font-medium">{event.tool}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-gray-500">
          {event.detail}
        </span>
        {event.diffStat ? (
          <span className="shrink-0 font-mono text-xs text-gray-500">{event.diffStat}</span>
        ) : null}
      </summary>
      <div className="border-t border-gray-100 px-3 py-2.5">
        {event.output ? (
          <pre
            className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-md px-3 py-2.5 font-mono text-xs leading-relaxed ${
              event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'
            }`}
          >
            {event.output.join('\n')}
          </pre>
        ) : (
          <p className="text-xs text-gray-400">Waiting for the tool result…</p>
        )}
      </div>
    </details>
  );
}

function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path strokeLinecap="round" d="M5 15V7a2 2 0 0 1 2-2h8" />
    </svg>
  );
}

function ForkIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <circle cx="6" cy="5" r="2" />
      <circle cx="18" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <path strokeLinecap="round" d="M6 7v10m12-10v3a4 4 0 0 1-4 4h-4" />
    </svg>
  );
}

function RewindIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 15 3 9l6-6M3 9h12a6 6 0 0 1 0 12h-3"
      />
    </svg>
  );
}

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    toast.success('Message copied');
  } catch {
    toast.error('Could not copy the message');
  }
}

function MessageActionButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/** Hover-revealed per-message actions; focus keeps them visible for keyboards. */
function MessageActions({
  align = 'start',
  children,
}: {
  align?: 'start' | 'end';
  children: ReactNode;
}) {
  return (
    <div
      className={`mt-1 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 ${
        align === 'end' ? 'justify-end' : ''
      }`}
    >
      {children}
    </div>
  );
}

function AssistantMessage({ text, extraActions }: { text: string; extraActions?: ReactNode }) {
  return (
    <div className="group py-3 text-gray-800">
      <Markdown text={text} />
      <MessageActions>
        <MessageActionButton label="Copy message" onClick={() => void copyMessage(text)}>
          <CopyIcon />
          Copy
        </MessageActionButton>
        {extraActions}
      </MessageActions>
    </div>
  );
}

function ThreadTurn({
  message,
  busy,
  onFork,
  onRewind,
}: {
  message: AgentThreadMessage;
  busy?: boolean;
  onFork?: () => void;
  onRewind?: () => void;
}) {
  if (message.role === 'assistant') {
    return (
      <AssistantMessage
        text={message.content}
        extraActions={
          onFork ? (
            <MessageActionButton label="Fork from here" onClick={onFork} disabled={busy}>
              <ForkIcon />
              Fork from here
            </MessageActionButton>
          ) : undefined
        }
      />
    );
  }
  return (
    <div className="group my-3 ml-auto w-fit max-w-[88%]">
      <div className="rounded-2xl bg-gray-100 px-4 py-3 text-sm leading-relaxed text-gray-800">
        <Markdown text={message.content} />
      </div>
      <MessageActions align="end">
        <MessageActionButton label="Copy message" onClick={() => void copyMessage(message.content)}>
          <CopyIcon />
          Copy
        </MessageActionButton>
        {onRewind ? (
          <MessageActionButton label="Edit & rewind" onClick={onRewind} disabled={busy}>
            <RewindIcon />
            Edit & rewind
          </MessageActionButton>
        ) : null}
      </MessageActions>
    </div>
  );
}

function EventRow({ event }: { event: AgentEvent }) {
  if (event.kind === 'lifecycle') return <LifecycleRow text={event.text} />;
  if (event.kind === 'thinking') {
    return (
      <div
        aria-label="Agent reasoning"
        className="flex items-center gap-2.5 py-2 text-[13px] text-gray-400"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-gray-300" />
        Agent is reasoning
      </div>
    );
  }
  if (event.kind === 'message') return <AssistantMessage text={event.text} />;
  if (event.kind === 'tool_use') return <ToolActivity event={event} />;
  if (event.kind === 'tool_result') {
    return (
      <details className="my-1 rounded-lg border border-gray-200 bg-white">
        <summary className="cursor-pointer px-3 py-2 text-[13px] text-gray-500">
          Unmatched tool result
        </summary>
        <pre className="m-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200">
          {event.text || 'Command completed with no output.'}
        </pre>
      </details>
    );
  }
  if (event.kind === 'terminal') {
    return (
      <pre className="my-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200">
        {event.text}
      </pre>
    );
  }
  if (event.kind === 'egress_denied') {
    return (
      <div className="my-2 flex items-center gap-2 rounded-lg bg-red-50 px-3 py-2 text-[13px] text-red-700">
        <span className="font-medium">Network request blocked</span>
        <span className="font-mono text-xs">{event.host}</span>
        <span className="text-red-500">× {event.attempts}</span>
      </div>
    );
  }
  return <p className="py-1 text-[11px] text-gray-400">{event.text}</p>;
}

function DetailStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-gray-500">{label}</dt>
      <dd className="break-all text-right text-gray-800">{value || '—'}</dd>
    </div>
  );
}

function DetailsDrawer({
  job,
  initialView,
  onClose,
}: {
  job: AgentJob;
  initialView: DrawerView;
  onClose: () => void;
}) {
  const [view, setView] = useState(initialView);
  return (
    <div className="fixed inset-0 z-[70] flex justify-end" role="dialog" aria-label="Run details">
      <button
        type="button"
        aria-label="Close run details"
        onClick={onClose}
        className="absolute inset-0 bg-gray-950/15"
      />
      <aside className="relative flex h-full w-full max-w-xl flex-col border-l border-gray-200 bg-white shadow-2xl">
        <div className="flex items-center border-b border-gray-200 px-5 py-4">
          <h2 className="font-semibold text-gray-900">Run details</h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-md p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            <svg
              aria-hidden="true"
              className="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex gap-1 border-b border-gray-100 px-4 pt-2 text-[13px] font-medium">
          {(
            [
              ['overview', 'Overview'],
              ['diff', `Changes${job.diffFiles.length ? ` · ${job.diffFiles.length}` : ''}`],
              ['raw', `Raw events · ${job.eventCount}`],
            ] as [DrawerView, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              className={`border-b-2 px-3 py-2 ${
                view === key
                  ? 'border-gray-900 text-gray-900'
                  : 'border-transparent text-gray-500 hover:text-gray-900'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {view === 'overview' ? (
            <div className="space-y-6">
              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                  Outcome
                </h3>
                <dl className="mt-2 divide-y divide-gray-100">
                  <DetailStat label="Branch" value={job.branch} />
                  <DetailStat label="Base" value={job.baseSha} />
                  <DetailStat label="Draft PR" value={job.prLabel ?? 'Not published'} />
                </dl>
              </section>
              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                  Usage
                </h3>
                <dl className="mt-2 divide-y divide-gray-100">
                  <DetailStat
                    label="Spend"
                    value={
                      job.hasLedger === false ? 'Ledger unavailable' : `$${job.spentUsd.toFixed(4)}`
                    }
                  />
                  <DetailStat label="Input tokens" value={job.usage.tokensIn} />
                  <DetailStat label="Output tokens" value={job.usage.tokensOut} />
                  <DetailStat label="Model calls" value={String(job.usage.turns)} />
                  <DetailStat
                    label="Budget"
                    value={job.budgetUsd ? `$${job.budgetUsd.toFixed(2)}` : '—'}
                  />
                </dl>
              </section>
              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                  Environment
                </h3>
                <dl className="mt-2 divide-y divide-gray-100">
                  <DetailStat label="Agent" value={job.runtime} />
                  <DetailStat label="Model" value={job.model} />
                  <DetailStat label="Sandbox" value={job.sandbox} />
                  <DetailStat label="Setup network" value={job.networkSetup} />
                  <DetailStat label="Agent network" value={job.networkAgent} />
                  <DetailStat label="Egress denials" value={String(job.egressDenials)} />
                </dl>
              </section>
              {job.gates.length ? (
                <section>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                    Publish checks
                  </h3>
                  <ul className="mt-2 space-y-2 text-[13px]">
                    {job.gates.map((gate) => (
                      <li key={gate.label} className="flex items-start gap-2 text-gray-600">
                        <span
                          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                            gate.state === 'pass'
                              ? 'bg-emerald-500'
                              : gate.state === 'hold'
                                ? 'bg-amber-400'
                                : 'bg-gray-300'
                          }`}
                        />
                        <span>
                          {gate.label}
                          {gate.detail ? (
                            <span className="ml-1 text-gray-400">{gate.detail}</span>
                          ) : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
              {job.attempts.length ? (
                <section>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400">
                    Attempts
                  </h3>
                  <ul className="mt-2 space-y-2 text-[13px] text-gray-600">
                    {job.attempts.map((attempt) => (
                      <li key={attempt.no} className="rounded-lg bg-gray-50 px-3 py-2">
                        Attempt {attempt.no} · {attempt.status}
                        {attempt.note ? (
                          <p className="mt-1 text-xs text-gray-400">{attempt.note}</p>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
            </div>
          ) : null}

          {view === 'diff' ? (
            job.diffLines.length ? (
              <div className="overflow-hidden rounded-lg border border-gray-200">
                <div className="flex flex-wrap gap-2 border-b border-gray-100 px-3 py-2 font-mono text-xs text-gray-500">
                  {job.diffFiles.map((file) => (
                    <span key={file}>{file}</span>
                  ))}
                  {job.diffStat ? (
                    <span className="ml-auto">
                      <span className="text-emerald-600">+{job.diffStat.add}</span>{' '}
                      <span className="text-red-500">−{job.diffStat.del}</span>
                    </span>
                  ) : null}
                </div>
                <pre className="overflow-x-auto font-mono text-xs leading-relaxed">
                  <code>
                    {job.diffLines.map((line, index) => (
                      <span key={index} className={`block px-3 ${DIFF_LINE_CLASS[line.marker]}`}>
                        {line.text}
                      </span>
                    ))}
                  </code>
                </pre>
              </div>
            ) : (
              <p className="text-sm text-gray-400">No changes yet.</p>
            )
          ) : null}

          {view === 'raw' ? (
            job.rawLines.length ? (
              <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg bg-gray-950 p-4 font-mono text-[11px] leading-relaxed text-gray-300">
                {job.rawLines.join('\n')}
              </pre>
            ) : (
              <p className="text-sm text-gray-400">No raw events yet.</p>
            )
          ) : null}
        </div>
      </aside>
    </div>
  );
}

function OutcomeCard({ job, onOpenDiff }: { job: AgentJob; onOpenDiff: () => void }) {
  if (job.state === 'running' || job.state === 'queued') return null;
  const successful = job.state === 'done' || job.state === 'needs_review';
  return (
    <div
      className={`mt-6 rounded-xl border px-4 py-3 ${
        successful ? 'border-emerald-200 bg-emerald-50/50' : 'border-gray-200 bg-gray-50'
      }`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900">
            {job.state === 'done'
              ? 'Draft pull request ready'
              : job.state === 'needs_review'
                ? 'Run completed'
                : job.state === 'failed'
                  ? 'Run failed'
                  : 'Run stopped'}
          </p>
          {job.stateNote ? <p className="mt-0.5 text-xs text-gray-500">{job.stateNote}</p> : null}
        </div>
        {job.diffFiles.length ? (
          <button
            type="button"
            onClick={onOpenDiff}
            className="rounded-md bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-200 hover:bg-gray-50"
          >
            View changes
          </button>
        ) : null}
        {job.prUrl ? (
          <a
            href={job.prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-800"
          >
            Open draft PR
          </a>
        ) : null}
      </div>
    </div>
  );
}

type GitView = 'diff' | 'review' | 'commits';

function GitPanel({ job, active }: { job: AgentJob; active: boolean }) {
  const [view, setView] = useState<GitView>('diff');
  const [live, setLive] = useState<AgentGitWorkspaceApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAgentJobGit(job.id)
      .then((result) => {
        if (!cancelled) setLive(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setLive(null);
          setError(cause instanceof Error ? cause.message : 'Could not load Git workspace');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active, job.id, job.state, refreshKey]);

  const files = useMemo(
    () => (live?.available ? toDiffFileDetails(live.patch) : (job.diffFileDetails ?? [])),
    [job.diffFileDetails, live],
  );
  const [selectedPath, setSelectedPath] = useState(files[0]?.path ?? '');
  const selected = files.find((file) => file.path === selectedPath) ?? files[0];
  const stat = useMemo(
    () =>
      files.reduce((total, file) => ({ add: total.add + file.add, del: total.del + file.del }), {
        add: 0,
        del: 0,
      }),
    [files],
  );

  useEffect(() => {
    if (files.length && !files.some((file) => file.path === selectedPath)) {
      setSelectedPath(files[0].path);
    }
  }, [files, selectedPath]);

  return (
    <div className="space-y-4" aria-label="Git workspace">
      <header className="flex flex-wrap items-center gap-3 border-b border-gray-200 pb-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-gray-800">{job.repo}</p>
          <p className="truncate font-mono text-xs text-gray-400">
            {live?.available ? live.branch || 'detached HEAD' : job.branch}
          </p>
        </div>
        <span className="rounded bg-gray-100 px-2 py-1 text-[11px] text-gray-500">
          {error
            ? 'Unavailable'
            : live?.available
              ? 'Live worktree'
              : loading
                ? 'Loading…'
                : 'Archived result'}
        </span>
        <button
          type="button"
          onClick={() => setRefreshKey((value) => value + 1)}
          disabled={loading}
          className="ml-auto rounded-md border border-gray-200 px-2.5 py-1 text-xs text-gray-500 hover:bg-gray-50 disabled:opacity-50"
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <div className="flex items-center gap-5 border-b border-gray-200" role="tablist">
        {(['diff', 'review', 'commits'] as GitView[]).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={view === item}
            onClick={() => setView(item)}
            className={`border-b-2 px-1 pb-2 text-xs font-medium capitalize ${
              view === item
                ? 'border-gray-900 text-gray-900'
                : 'border-transparent text-gray-400 hover:text-gray-700'
            }`}
          >
            {item}
          </button>
        ))}
        {view === 'diff' && files.length ? (
          <span className="ml-auto pb-2 font-mono text-[11px]">
            <span className="text-emerald-600">+{stat.add}</span>{' '}
            <span className="text-red-500">−{stat.del}</span>
          </span>
        ) : null}
      </div>

      {error ? <p className="text-xs text-red-600">{error}</p> : null}

      {view === 'diff' && files.length ? (
        <section className="overflow-hidden rounded-xl border border-gray-200 lg:grid lg:grid-cols-[minmax(14rem,0.35fr)_minmax(0,1fr)]">
          <div className="border-b border-gray-200 bg-gray-50/60 lg:border-b-0 lg:border-r">
            <div className="flex items-center px-3 py-2.5 text-xs text-gray-500">
              <span>
                {files.length} changed file{files.length === 1 ? '' : 's'}
              </span>
              <span className="ml-auto font-mono">Changes</span>
            </div>
            <div className="max-h-[34rem] overflow-auto border-t border-gray-200">
              {files.map((file) => (
                <button
                  key={file.path}
                  type="button"
                  onClick={() => setSelectedPath(file.path)}
                  className={`flex w-full items-center gap-3 border-b border-gray-100 px-3 py-2.5 text-left text-xs last:border-b-0 ${
                    selected?.path === file.path
                      ? 'bg-white text-gray-900'
                      : 'text-gray-600 hover:bg-white'
                  }`}
                >
                  <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  <span className="shrink-0 font-mono">
                    <span className="text-emerald-600">+{file.add}</span>{' '}
                    <span className="text-red-500">−{file.del}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="min-w-0 bg-white">
            <div className="border-b border-gray-100 px-4 py-2.5 font-mono text-xs text-gray-600">
              {selected?.path}
            </div>
            <pre className="max-h-[34rem] overflow-auto font-mono text-xs leading-relaxed">
              <code>
                {selected?.lines.map((line, index) => (
                  <span
                    key={index}
                    className={`block min-w-max px-4 ${DIFF_LINE_CLASS[line.marker]}`}
                  >
                    {line.text || ' '}
                  </span>
                ))}
              </code>
            </pre>
          </div>
        </section>
      ) : view === 'diff' ? (
        <div className="rounded-xl border border-dashed border-gray-200 px-5 py-12 text-center text-sm text-gray-400">
          No changes yet.
        </div>
      ) : view === 'review' ? (
        <section className="space-y-4 rounded-xl border border-gray-200 p-5">
          <div className="flex items-center gap-3">
            <div>
              <p className="text-sm font-medium text-gray-800">
                {files.length} changed file{files.length === 1 ? '' : 's'}
              </p>
              <p className="mt-1 text-xs text-gray-400">
                Compared with {job.baseRef || job.baseSha || 'the pinned base'}
              </p>
            </div>
            <p className="ml-auto font-mono text-xs">
              <span className="text-emerald-600">+{stat.add}</span>{' '}
              <span className="text-red-500">−{stat.del}</span>
            </p>
          </div>
          {files.length ? (
            <ul className="divide-y divide-gray-100 border-y border-gray-100">
              {files.map((file) => (
                <li key={file.path} className="flex gap-3 py-2.5 font-mono text-xs">
                  <span className="min-w-0 flex-1 truncate text-gray-600">{file.path}</span>
                  <span className="text-emerald-600">+{file.add}</span>
                  <span className="text-red-500">−{file.del}</span>
                </li>
              ))}
            </ul>
          ) : null}
          {job.prUrl ? (
            <a
              href={job.prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800"
            >
              Open pull request
            </a>
          ) : (
            <p className="text-xs text-gray-400">Pull request not published yet.</p>
          )}
        </section>
      ) : (
        <section className="overflow-hidden rounded-xl border border-gray-200">
          {live?.available && live.commits.length ? (
            <ul className="divide-y divide-gray-100">
              {live.commits.map((commit) => (
                <li key={commit.sha} className="px-4 py-3">
                  <div className="flex items-start gap-3">
                    <span className="font-mono text-xs text-gray-400">{commit.short_sha}</span>
                    <div className="min-w-0">
                      <p className="truncate text-sm text-gray-700">{commit.subject}</p>
                      <p className="mt-1 text-[11px] text-gray-400">
                        {commit.author} · {commit.authored_at}
                      </p>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-5 py-12 text-center text-sm text-gray-400">
              Commit history is available for live workspaces.
            </p>
          )}
        </section>
      )}
    </div>
  );
}

export function JobDetail({ job, onReload }: { job: AgentJob; onReload?: () => void }) {
  const router = useRouter();
  const liveAttempt = useMemo(
    () => (job.attempts.length > 0 ? job.attempts[job.attempts.length - 1].no : 0),
    [job.attempts],
  );
  const [attemptNo, setAttemptNo] = useState(liveAttempt);
  const [drawer, setDrawer] = useState<DrawerView | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('git');
  const [followUp, setFollowUp] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [forking, setForking] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const workspacePane = useResizablePane({
    storageKey: WORKSPACE_WIDTH_STORAGE_KEY,
    defaultWidth: WORKSPACE_DEFAULT_WIDTH,
    minWidth: WORKSPACE_MIN_WIDTH,
    maxWidth: WORKSPACE_MAX_WIDTH,
    side: 'end',
    maxViewportFraction: 0.75,
    siblingMinWidth: TRANSCRIPT_MIN_WIDTH,
  });

  useEffect(() => setAttemptNo(liveAttempt), [liveAttempt]);

  // An "Edit & rewind" fork leaves the edited prompt behind for its landing
  // page. Session-scoped and consumed once, so a reload does not resurrect it.
  useEffect(() => {
    const key = `agent-rewind-draft-${job.id}`;
    let draft: string | null = null;
    try {
      draft = sessionStorage.getItem(key);
      if (draft) sessionStorage.removeItem(key);
    } catch {
      return;
    }
    if (draft) setFollowUp(draft);
  }, [job.id]);

  const selectedAttempt = job.attempts.find((attempt) => attempt.no === attemptNo);
  const visibleEvents = job.events.filter(
    (event) => event.attemptNo === undefined || attemptNo === 0 || event.attemptNo === attemptNo,
  );
  const pill = STATE_PILL[job.state];
  const isActive = job.state === 'running' || job.state === 'queued';

  function openWorkspace(tab: WorkspaceTab) {
    setWorkspaceTab(tab);
    setWorkspaceOpen(true);
  }

  async function stop() {
    setStopping(true);
    setActionError(null);
    try {
      await cancelAgentJob(job.id);
      onReload?.();
    } catch (cause: unknown) {
      setActionError(cause instanceof Error ? cause.message : 'Could not stop this run');
    } finally {
      setStopping(false);
    }
  }

  async function submitFollowUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = followUp.trim();
    if (!prompt || submitting) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const child = await followUpAgentJob(job.id, { prompt });
      router.push(`/agents/${child.id}`);
    } catch (cause: unknown) {
      setActionError(cause instanceof Error ? cause.message : 'Could not queue the follow-up');
      setSubmitting(false);
    }
  }

  // Fork duplicates the conversation up to the anchor turn into a new thread
  // and navigates there; `draft` prefills the composer for edit-and-rewind.
  async function forkFrom(anchorJobId: string, draft?: string) {
    if (forking) return;
    setForking(true);
    setActionError(null);
    try {
      const fork = await forkAgentJob(anchorJobId);
      if (draft) {
        try {
          sessionStorage.setItem(`agent-rewind-draft-${fork.id}`, draft);
        } catch {
          // Blocked storage only loses the prefill, never the fork itself.
        }
      }
      toast.success('Conversation forked');
      router.push(`/agents/${fork.id}`);
    } catch (cause: unknown) {
      setActionError(cause instanceof Error ? cause.message : 'Could not fork the conversation');
      setForking(false);
    }
  }

  const threadMessages = job.threadMessages ?? [];
  const lastHistoryJobId = threadMessages.length
    ? threadMessages[threadMessages.length - 1].jobId
    : undefined;

  return (
    <section className="flex h-full min-h-0 flex-col bg-white">
      <header className="z-20 shrink-0 border-b border-gray-100 bg-white/95 px-5 backdrop-blur">
        <div
          className={`mx-auto flex w-full items-center gap-3 py-3 ${
            workspaceOpen ? 'max-w-[100rem]' : 'max-w-4xl'
          }`}
        >
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[15px] font-semibold text-gray-900">{job.title}</h1>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-gray-500">
              <svg
                aria-hidden="true"
                className="h-3.5 w-3.5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
                />
              </svg>
              <span className="truncate">{job.repo}</span>
              {job.turnNo && job.turnNo > 1 ? <span>· turn {job.turnNo}</span> : null}
              <span aria-hidden="true">·</span>
              <button
                type="button"
                onClick={() => setDrawer('overview')}
                className="shrink-0 font-medium text-gray-500 hover:text-gray-900 hover:underline"
              >
                Run details
              </button>
            </div>
          </div>
          <span
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${pill.className}`}
          >
            {pill.dot ? (
              <span
                className={`h-1.5 w-1.5 rounded-full ${pill.dot} ${isActive ? 'animate-pulse' : ''}`}
              />
            ) : null}
            {job.state === 'queued' && job.parentJobId ? 'Queued after current run' : pill.label}
          </span>
          {job.diffFiles.length ? (
            <button
              type="button"
              onClick={() => openWorkspace('git')}
              className="hidden rounded-md px-2.5 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100 sm:block"
            >
              Changes
              <span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[11px]">
                {job.diffFiles.length}
              </span>
            </button>
          ) : null}
          {isActive ? (
            <button
              type="button"
              onClick={() => void stop()}
              disabled={stopping}
              className="inline-flex items-center gap-1.5 rounded-md bg-gray-900 px-2.5 py-1.5 text-[13px] font-medium text-white hover:bg-gray-800 disabled:opacity-60"
            >
              <span className="h-2.5 w-2.5 rounded-sm bg-white" />
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => setWorkspaceOpen((current) => !current)}
            aria-label={workspaceOpen ? 'Close workspace' : 'Open workspace'}
            aria-expanded={workspaceOpen}
            aria-controls="job-workspace-pane"
            className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border transition-colors ${
              workspaceOpen
                ? 'border-gray-900 bg-gray-900 text-white hover:bg-gray-800'
                : 'border-gray-200 text-gray-600 hover:bg-gray-100 hover:text-gray-900'
            }`}
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.8}
            >
              <rect x="3.5" y="4" width="17" height="16" rx="2" />
              <path d="M14.5 4v16" />
            </svg>
          </button>
        </div>
      </header>

      <div
        ref={workspacePane.containerRef}
        className={`min-h-0 flex-1 overflow-hidden ${workspaceOpen ? 'block lg:flex' : 'flex'}`}
      >
        <div
          role="region"
          aria-label="Task progress"
          className={`min-w-0 flex-1 flex-col overflow-y-auto ${
            workspaceOpen ? 'hidden lg:flex' : 'flex'
          }`}
        >
          <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-5 pb-6 pt-6">
            <div className="flex-1">
              {threadMessages.map((message, index) => {
                const previous = index > 0 ? threadMessages[index - 1] : undefined;
                const rewindAnchor =
                  message.role === 'user' && previous && previous.jobId !== message.jobId
                    ? previous.jobId
                    : undefined;
                return (
                  <ThreadTurn
                    key={message.id}
                    message={message}
                    busy={forking}
                    onFork={
                      message.role === 'assistant' ? () => void forkFrom(message.jobId) : undefined
                    }
                    onRewind={
                      rewindAnchor ? () => void forkFrom(rewindAnchor, message.content) : undefined
                    }
                  />
                );
              })}

              {job.historyIncludesPrompt ? null : (
                <div className="group my-4 rounded-xl border border-gray-200 bg-white px-4 py-3 shadow-sm">
                  <Markdown text={job.prompt || job.title} />
                  <MessageActions align="end">
                    <MessageActionButton
                      label="Copy message"
                      onClick={() => void copyMessage(job.prompt || job.title)}
                    >
                      <CopyIcon />
                      Copy
                    </MessageActionButton>
                    {lastHistoryJobId && lastHistoryJobId !== job.id ? (
                      <MessageActionButton
                        label="Edit & rewind"
                        onClick={() => void forkFrom(lastHistoryJobId, job.prompt || job.title)}
                        disabled={forking}
                      >
                        <RewindIcon />
                        Edit &amp; rewind
                      </MessageActionButton>
                    ) : null}
                  </MessageActions>
                </div>
              )}

              {job.stateNote && job.state !== 'failed' ? (
                <p className="mb-2 text-xs text-gray-400">{job.stateNote}</p>
              ) : null}

              {job.attempts.length > 1 ? (
                <div className="my-3 flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="mr-1 text-gray-400">Run attempts</span>
                  {job.attempts.map((attempt) => (
                    <button
                      key={attempt.no}
                      type="button"
                      onClick={() => setAttemptNo(attempt.no)}
                      className={`rounded-md px-2 py-1 ${
                        attempt.no === attemptNo
                          ? 'bg-gray-900 text-white'
                          : 'bg-gray-100 text-gray-500 hover:bg-gray-200'
                      }`}
                    >
                      {attempt.no} · {attempt.status}
                    </button>
                  ))}
                </div>
              ) : null}

              {selectedAttempt?.status === 'superseded' && selectedAttempt.note ? (
                <div className="my-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-800">
                  {selectedAttempt.note}
                </div>
              ) : null}

              <div aria-label="Agent activity" className="mt-2">
                {visibleEvents.map((event, index) => (
                  <EventRow key={`${event.attemptNo ?? 0}-${index}`} event={event} />
                ))}
                {job.liveNote ? (
                  <div className="flex items-center gap-2.5 py-3 text-[13px] text-gray-500">
                    <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                    {job.liveNote}
                  </div>
                ) : null}
                {job.state === 'queued' && visibleEvents.length === 0 ? (
                  <div className="flex items-center gap-2.5 py-3 text-[13px] text-gray-500">
                    <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400" />
                    {job.parentJobId ? 'Queued after the current run' : 'Waiting for a runner'}
                  </div>
                ) : null}
              </div>

              <OutcomeCard job={job} onOpenDiff={() => openWorkspace('git')} />
            </div>

            <div className="sticky bottom-0 z-10 -mx-2 mt-10 bg-gradient-to-t from-white via-white px-2 pb-2 pt-8">
              <form
                onSubmit={(event) => void submitFollowUp(event)}
                className="rounded-2xl border border-gray-200 bg-white shadow-lg shadow-gray-200/50 focus-within:border-gray-300"
              >
                <textarea
                  rows={2}
                  value={followUp}
                  onChange={(event) => setFollowUp(event.target.value)}
                  placeholder="Add a follow-up"
                  aria-label="Add a follow-up"
                  className="w-full resize-none rounded-t-2xl border-0 bg-transparent px-4 pt-3 text-sm leading-relaxed text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0"
                />
                <div className="flex items-center gap-2 px-3 pb-2.5">
                  <span className="min-w-0 flex-1 truncate text-[11px] text-gray-400">
                    Inherits {job.runtime} · {job.model}
                    {isActive ? ' · queued after this run' : ''}
                  </span>
                  <button
                    type="submit"
                    disabled={!followUp.trim() || submitting}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-gray-900 text-white hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-40"
                    aria-label="Send follow-up"
                  >
                    {submitting ? (
                      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                    ) : (
                      <svg
                        aria-hidden="true"
                        className="h-4 w-4"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={2}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M12 19V5m0 0-6 6m6-6 6 6"
                        />
                      </svg>
                    )}
                  </button>
                </div>
              </form>
              {actionError ? (
                <p className="mt-2 text-center text-xs text-red-600" role="alert">
                  {actionError}
                </p>
              ) : null}
            </div>
          </div>
        </div>

        {workspaceOpen ? (
          <PaneResizer
            pane={workspacePane}
            label="Resize workspace"
            controls="job-workspace-pane"
            className="hidden lg:block"
          />
        ) : null}

        <aside
          id="job-workspace-pane"
          role="region"
          aria-label="Job workspace"
          hidden={!workspaceOpen}
          style={{ '--job-workspace-width': `${workspacePane.width}px` } as CSSProperties}
          className={`${
            workspaceOpen ? 'flex' : 'hidden'
          } min-h-0 min-w-0 flex-col overflow-hidden bg-white lg:w-[var(--job-workspace-width)] lg:shrink-0`}
        >
          <nav
            aria-label="Workspace views"
            role="tablist"
            className="sticky top-0 z-10 flex gap-1 overflow-x-auto border-b border-gray-200 bg-white px-4 text-[13px] font-medium"
          >
            {WORKSPACE_TABS.map((tab) => (
              <button
                key={tab.key}
                id={`workspace-tab-${tab.key}`}
                type="button"
                role="tab"
                aria-controls={`workspace-panel-${tab.key}`}
                aria-selected={workspaceTab === tab.key}
                onClick={() => openWorkspace(tab.key)}
                className={`shrink-0 border-b-2 px-3 py-3 ${
                  workspaceTab === tab.key
                    ? 'border-gray-900 text-gray-900'
                    : 'border-transparent text-gray-500 hover:text-gray-900'
                }`}
              >
                {tab.label}
                {tab.key === 'git' && job.diffFiles.length ? (
                  <span className="ml-1.5 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">
                    {job.diffFiles.length}
                  </span>
                ) : null}
              </button>
            ))}
          </nav>
          <div
            id="workspace-panel-git"
            role="tabpanel"
            aria-labelledby="workspace-tab-git"
            hidden={workspaceTab !== 'git'}
            className="min-h-0 flex-1 overflow-y-auto p-5"
          >
            <GitPanel job={job} active={workspaceOpen && workspaceTab === 'git'} />
          </div>
          <div
            id="workspace-panel-terminal"
            role="tabpanel"
            aria-labelledby="workspace-tab-terminal"
            hidden={workspaceTab !== 'terminal'}
            className="min-h-0 flex-1 p-3"
          >
            <TerminalWorkspace
              key={job.id}
              jobId={job.id}
              active={workspaceOpen && workspaceTab === 'terminal'}
              disabled={isActive}
              disabledReason="Terminal input is available after the agent finishes, so both do not modify the workspace at once."
            />
          </div>
          <div
            id="workspace-panel-files"
            role="tabpanel"
            aria-labelledby="workspace-tab-files"
            hidden={workspaceTab !== 'files'}
            className="min-h-0 flex-1 overflow-y-auto p-5"
          >
            <WorkspaceFiles job={job} active={workspaceOpen && workspaceTab === 'files'} />
          </div>
        </aside>
      </div>

      {drawer ? (
        <DetailsDrawer job={job} initialView={drawer} onClose={() => setDrawer(null)} />
      ) : null}
    </section>
  );
}
