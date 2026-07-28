'use client';

import { useMemo, useState } from 'react';
import type { AgentEvent, AgentJob } from './types';

type DetailTab = 'stream' | 'diff' | 'raw';

const STATE_PILL: Record<AgentJob['state'], { label: string; className: string; dot?: string }> = {
  running: { label: 'Running', className: 'bg-blue-50 text-blue-700', dot: 'bg-blue-500' },
  queued: { label: 'Queued', className: 'bg-gray-100 text-gray-600' },
  needs_review: { label: 'Needs review', className: 'bg-amber-50 text-amber-700' },
  done: { label: 'Done', className: 'bg-emerald-50 text-emerald-700' },
  failed: { label: 'Failed', className: 'bg-red-50 text-red-600' },
  cancelled: { label: 'Cancelled', className: 'bg-gray-100 text-gray-500' },
};

function ToolIcon({ tool }: { tool: 'Read' | 'Bash' | 'Edit' }) {
  const paths: Record<string, string> = {
    Read: 'M12 6.25c-2.5-2-6.5-2-9 0v12c2.5-2 6.5-2 9 0 2.5-2 6.5-2 9 0v-12c-2.5-2-6.5-2-9 0Zm0 0v12',
    Bash: 'm7 8 4 4-4 4m6 0h4M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z',
    Edit: 'M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z',
  };
  return (
    <svg
      className="h-4 w-4 shrink-0 text-gray-400"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={paths[tool]} />
    </svg>
  );
}

// if-chain rather than switch: the repo's `indent` ESLint rule and Prettier
// disagree on switch-case bodies in TSX.
function EventRow({ event }: { event: AgentEvent }) {
  if (event.kind === 'lifecycle') {
    return (
      <div className="flex items-center gap-3 px-4 py-2.5 text-[13px] text-gray-400">
        <svg
          className="h-4 w-4 shrink-0"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
        {event.text}
      </div>
    );
  }
  if (event.kind === 'thinking') {
    return (
      <div className="flex items-start gap-3 px-4 py-2.5">
        <svg
          className="mt-0.5 h-4 w-4 shrink-0 text-gray-300"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 3a6 6 0 0 0-6 6c0 2.22 1.21 4.16 3 5.2V17a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1v-2.8c1.79-1.04 3-2.98 3-5.2a6 6 0 0 0-6-6ZM10 21h4"
          />
        </svg>
        <p className="font-serif text-[15px] italic leading-snug text-gray-500">{event.text}</p>
      </div>
    );
  }
  if (event.kind === 'message') {
    return <p className="px-4 py-2.5 text-[13px] leading-relaxed text-gray-700">{event.text}</p>;
  }
  if (event.kind === 'tool_use') {
    return (
      <div className="px-4 py-2.5">
        <div className="flex items-center gap-3 text-[13px]">
          <ToolIcon tool={event.tool} />
          <span className="font-medium text-gray-700">{event.tool}</span>
          <span className="truncate font-mono text-xs text-gray-500">{event.detail}</span>
          {event.diffStat && (
            <span className="ml-auto font-mono text-xs text-gray-500">{event.diffStat}</span>
          )}
        </div>
        {event.output && (
          <pre className="mt-2 overflow-x-auto rounded-lg bg-gray-950 px-4 py-3 font-mono text-xs leading-relaxed text-gray-300">
            {event.output.join('\n')}
          </pre>
        )}
      </div>
    );
  }
  if (event.kind === 'egress_denied') {
    return (
      <div className="flex items-center gap-3 bg-red-50/50 px-4 py-2.5 text-[13px]">
        <svg
          className="h-4 w-4 shrink-0 text-red-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M18.36 5.64 5.64 18.36M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z"
          />
        </svg>
        <span className="font-medium text-red-700">Egress blocked</span>
        <span className="font-mono text-xs text-red-600">{event.host}</span>
        <span className="text-xs text-red-500">
          × {event.attempts} · agent tier is PlatformOnly
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3 px-4 py-1.5 text-[11px] text-gray-400">
      <span className="w-4" /> {event.text}
    </div>
  );
}

function GateIcon({ state }: { state: 'pass' | 'pending' | 'hold' }) {
  if (state === 'pass') {
    return (
      <svg
        className="h-4 w-4 shrink-0 text-emerald-500"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2.5}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
      </svg>
    );
  }
  if (state === 'hold') {
    return (
      <svg
        className="h-4 w-4 shrink-0 text-amber-500"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M16.5 10.5V7a4.5 4.5 0 0 0-9 0v3.5m-.75 11h10.5a2.25 2.25 0 0 0 2.25-2.25v-6.75a2.25 2.25 0 0 0-2.25-2.25H6.75a2.25 2.25 0 0 0-2.25 2.25v6.75a2.25 2.25 0 0 0 2.25 2.25Z"
        />
      </svg>
    );
  }
  return <span className="inline-block h-4 w-4 shrink-0 rounded-full border-2 border-gray-200" />;
}

const DIFF_LINE_CLASS = {
  hunk: 'bg-gray-50 py-1 text-gray-400',
  ctx: 'text-gray-600',
  add: 'bg-emerald-50 text-emerald-700',
  del: 'bg-red-50 text-red-700',
} as const;

export function JobDetail({ job }: { job: AgentJob }) {
  const [tab, setTab] = useState<DetailTab>('stream');
  const liveAttempt = useMemo(
    () => (job.attempts.length > 0 ? job.attempts[job.attempts.length - 1].no : 0),
    [job.attempts],
  );
  const [attemptNo, setAttemptNo] = useState(liveAttempt);
  const selectedAttempt = job.attempts.find((attempt) => attempt.no === attemptNo);
  const pill = STATE_PILL[job.state];
  const budgetPct = Math.min(100, Math.round((job.spentUsd / job.budgetUsd) * 100));

  return (
    <section className="mx-auto w-full max-w-5xl px-6 py-6">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${pill.className}`}
        >
          {pill.dot && <span className={`h-1.5 w-1.5 animate-pulse rounded-full ${pill.dot}`} />}
          {pill.label}
        </span>
        <h1 className="min-w-0 flex-1 truncate text-xl font-bold text-gray-900">{job.title}</h1>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            title="Skeleton — wired to the Job API in P0 (issue #1041)"
            className="rounded-md bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
          >
            Clone with different model
          </button>
          {job.state === 'running' && (
            <button
              type="button"
              title="Skeleton — wired to the Job API in P0 (issue #1041)"
              className="rounded-md bg-white px-3 py-1.5 text-[13px] font-medium text-red-600 shadow-sm ring-1 ring-inset ring-red-200 hover:bg-red-50"
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-gray-500">
        <span className="font-mono">
          {job.repo} @ {job.baseSha} → {job.branch}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-crimson" />
          {job.runtime}
          {job.runtimeVersion ? ` ${job.runtimeVersion}` : ''}
        </span>
        <span>
          {job.model}{' '}
          {job.modelLocal && <span className="text-emerald-600">local · {job.modelLocal}</span>}
        </span>
        <span
          className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-600"
          title="egress: gateway + event stream only"
        >
          agent net: {job.networkAgent}
        </span>
        <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-600">
          sandbox: {job.sandbox}
        </span>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-center gap-2.5 text-xs text-gray-600">
          <span>Budget</span>
          <span className="h-1.5 w-32 overflow-hidden rounded-full bg-gray-200">
            <span
              className="block h-full rounded-full bg-gray-900"
              style={{ width: `${budgetPct}%` }}
            />
          </span>
          <span className="font-medium text-gray-900">${job.spentUsd.toFixed(2)}</span>
          <span className="text-gray-400">
            / ${job.budgetUsd.toFixed(2)}
            {job.elapsedLabel ? ` · ${job.elapsedLabel} / ${job.timeoutLabel}` : ''}
          </span>
        </div>
        {job.attempts.length > 0 && (
          <div className="flex items-center gap-1 text-xs">
            <span className="mr-1 text-gray-500">Attempts</span>
            {job.attempts.map((attempt) => (
              <button
                key={attempt.no}
                type="button"
                onClick={() => setAttemptNo(attempt.no)}
                className={
                  attempt.no === attemptNo
                    ? 'rounded-md bg-gray-900 px-2 py-0.5 font-medium text-white'
                    : 'rounded-md px-2 py-0.5 font-medium text-gray-500 ring-1 ring-inset ring-gray-200 hover:bg-gray-100'
                }
              >
                {attempt.no} · {attempt.status}
              </button>
            ))}
          </div>
        )}
      </div>

      {selectedAttempt?.status === 'superseded' && selectedAttempt.note && (
        <div className="mt-3 flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-800">
          <svg
            className="mt-0.5 h-4 w-4 shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"
            />
          </svg>
          <span>{selectedAttempt.note}</span>
        </div>
      )}

      <div className="mt-5 flex flex-col gap-5 xl:flex-row">
        <div className="min-w-0 flex-1 self-start overflow-hidden rounded-xl bg-white shadow-sm ring-1 ring-gray-200">
          <div className="flex items-center gap-1 border-b border-gray-100 px-4 pb-0 pt-2.5 text-[13px] font-medium">
            {(
              [
                ['stream', 'Stream'],
                ['diff', 'Diff'],
                ['raw', 'Raw log'],
              ] as [DetailTab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`border-b-2 px-3 py-2 ${
                  tab === key
                    ? 'border-gray-900 text-gray-900'
                    : 'border-transparent text-gray-500 hover:text-gray-900'
                }`}
              >
                {label}
                {key === 'diff' && job.diffFiles.length > 0 && (
                  <span className="ml-1 rounded bg-gray-100 px-1.5 text-[11px] text-gray-500">
                    {job.diffFiles.length} files
                  </span>
                )}
              </button>
            ))}
            <span
              className="ml-auto pb-1 text-[11px] text-gray-400"
              title="SSE with Last-Event-ID resume"
            >
              {job.state === 'running' ? 'live · ' : ''}event #{job.eventCount}
            </span>
          </div>

          {tab === 'stream' && (
            <div className="divide-y divide-gray-50">
              {job.events.map((event, index) => (
                <EventRow key={index} event={event} />
              ))}
              {job.liveNote && (
                <div className="flex items-center gap-3 px-4 py-3 text-[13px] text-gray-500">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                  {job.liveNote}
                </div>
              )}
            </div>
          )}

          {tab === 'diff' &&
            (job.diffLines.length > 0 ? (
              <div>
                <div className="flex items-center gap-4 border-b border-gray-100 px-4 py-2 font-mono text-xs text-gray-500">
                  {job.diffFiles.map((file, index) => (
                    <span key={file} className={index === 0 ? 'text-gray-800' : ''}>
                      {file}
                    </span>
                  ))}
                  {job.diffStat && (
                    <span className="ml-auto">
                      <span className="text-emerald-600">+{job.diffStat.add}</span>{' '}
                      <span className="text-red-500">−{job.diffStat.del}</span>
                    </span>
                  )}
                </div>
                <pre className="overflow-x-auto font-mono text-xs leading-relaxed">
                  <code>
                    {job.diffLines.map((line, index) => (
                      <span key={index} className={`block px-4 ${DIFF_LINE_CLASS[line.marker]}`}>
                        {line.text}
                      </span>
                    ))}
                  </code>
                </pre>
              </div>
            ) : (
              <p className="px-4 py-6 text-[13px] text-gray-400">No diff yet.</p>
            ))}

          {tab === 'raw' && (
            <div>
              {job.rawLines.length > 0 ? (
                <pre className="overflow-x-auto px-4 py-3 font-mono text-[11px] leading-relaxed text-gray-500">
                  {job.rawLines.join('\n')}
                </pre>
              ) : (
                <p className="px-4 py-6 text-[13px] text-gray-400">No raw events yet.</p>
              )}
              <div className="border-t border-gray-100 px-4 py-2 text-[11px] text-gray-400">
                Tier 1 runtime: normalized events + raw. Tier 2 runtimes (pi, OpenCode) show this
                pane only.
              </div>
            </div>
          )}
        </div>

        <div className="w-full shrink-0 space-y-4 xl:w-72">
          <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200">
            <h2 className="text-[13px] font-semibold text-gray-900">Outcome</h2>
            <div className="mt-2.5 space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Branch</span>
                <span className="font-mono text-xs text-gray-800">{job.branch}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Base</span>
                <span className="font-mono text-xs text-gray-800">{job.baseSha}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Draft PR</span>
                {job.prLabel ? (
                  <span className="font-mono text-xs text-gray-800">{job.prLabel}</span>
                ) : (
                  <span className="text-xs text-gray-400">after publish gates</span>
                )}
              </div>
            </div>
            <button
              type="button"
              disabled
              className="mt-3 w-full rounded-lg bg-gray-100 px-3 py-1.5 text-[13px] font-medium text-gray-400"
            >
              {job.prLabel ? 'Published' : 'Publish — waiting for run'}
            </button>
          </div>

          {job.gates.length > 0 && (
            <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200">
              <h2 className="text-[13px] font-semibold text-gray-900">Publish gates</h2>
              <ul className="mt-2.5 space-y-2 text-[13px]">
                {job.gates.map((gate) => (
                  <li
                    key={gate.label}
                    className={`flex items-center gap-2 ${
                      gate.state === 'pending' ? 'text-gray-400' : 'text-gray-600'
                    }`}
                    title={gate.state === 'hold' ? gate.detail : undefined}
                  >
                    <GateIcon state={gate.state} />
                    {gate.label}
                    {gate.detail && gate.state !== 'hold' && (
                      <span className="text-gray-400">{gate.detail}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200">
            <h2 className="text-[13px] font-semibold text-gray-900">Usage</h2>
            <div className="mt-1.5 text-xl font-bold text-gray-900">${job.spentUsd.toFixed(2)}</div>
            <div className="mt-0.5 text-xs text-gray-500">
              {job.usage.tokensIn} in · {job.usage.tokensOut} out · cache {job.usage.cachePct}%
            </div>
            <p className="mt-2.5 rounded-md bg-gray-50 px-2.5 py-2 text-[11px] leading-snug text-gray-500">
              Metered by the gateway&apos;s <span className="font-mono">api_logs</span> under a
              job-scoped key — never agent-reported.
            </p>
          </div>

          <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200">
            <h2 className="text-[13px] font-semibold text-gray-900">Sandbox</h2>
            <div className="mt-2.5 space-y-2 text-[13px]">
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Isolation</span>
                <span className="text-gray-800">{job.sandbox}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Git credentials</span>
                <span className="text-emerald-600">none in sandbox</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Net · setup / agent</span>
                <span className="text-gray-800">
                  {job.networkSetup} / {job.networkAgent}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-500">Egress denials</span>
                {job.egressDenials > 0 ? (
                  <span className="font-medium text-red-600">{job.egressDenials}</span>
                ) : (
                  <span className="text-gray-800">0</span>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
