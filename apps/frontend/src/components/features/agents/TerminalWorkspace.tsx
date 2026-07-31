'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  createAgentTerminal,
  deleteAgentTerminal,
  listAgentTerminals,
  type AgentTerminalApi,
} from '@/lib/api/agents';

import { TerminalPane } from './TerminalPane';

const MAX_SESSIONS = 4;

interface TerminalWorkspaceProps {
  jobId: string;
  active: boolean;
  ready?: boolean;
}

export function TerminalWorkspace({ jobId, active, ready = true }: TerminalWorkspaceProps) {
  const [sessions, setSessions] = useState<AgentTerminalApi[]>([]);
  const [visibleIds, setVisibleIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadedJobRef = useRef<string | null>(null);
  const currentJobRef = useRef(jobId);
  const jobGenerationRef = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function requestIsCurrent(requestJobId: string, generation: number): boolean {
    return (
      mountedRef.current &&
      currentJobRef.current === requestJobId &&
      jobGenerationRef.current === generation
    );
  }

  useEffect(() => {
    currentJobRef.current = jobId;
    jobGenerationRef.current += 1;
    setSessions([]);
    setVisibleIds([]);
    setError(null);
    setLoadFailed(false);
    setLoading(false);
    setBusy(false);
    loadedJobRef.current = null;
  }, [jobId]);

  useEffect(() => {
    if (!active || !ready || loadFailed || loadedJobRef.current === jobId) return;
    const generation = jobGenerationRef.current;
    loadedJobRef.current = jobId;
    setLoading(true);
    void listAgentTerminals(jobId)
      .then((items) => {
        if (!requestIsCurrent(jobId, generation)) return;
        setSessions(items);
        if (items.length) setVisibleIds([items[0].id]);
      })
      .catch((cause: unknown) => {
        if (requestIsCurrent(jobId, generation)) {
          loadedJobRef.current = null;
          setLoadFailed(true);
          setError(cause instanceof Error ? cause.message : 'Could not load terminals');
        }
      })
      .finally(() => {
        if (requestIsCurrent(jobId, generation)) setLoading(false);
      });
  }, [active, jobId, loadFailed, ready]);

  const visible = useMemo(
    () => visibleIds.map((id) => sessions.find((session) => session.id === id)).filter(Boolean),
    [sessions, visibleIds],
  ) as AgentTerminalApi[];

  const awaitingInitialLoad = active && !loadFailed && (!ready || loadedJobRef.current !== jobId);
  const canCreate =
    !loadFailed && !loading && !awaitingInitialLoad && sessions.length < MAX_SESSIONS;

  function retryLoad() {
    loadedJobRef.current = null;
    setError(null);
    setLoadFailed(false);
  }

  async function create(mode: 'new' | 'split', paneIndex = 0) {
    if (!canCreate || busy) return;
    const requestJobId = jobId;
    const generation = jobGenerationRef.current;
    setBusy(true);
    setError(null);
    try {
      const session = await createAgentTerminal(requestJobId);
      if (!requestIsCurrent(requestJobId, generation)) {
        void deleteAgentTerminal(requestJobId, session.id).catch(() => {
          // The old workspace may already have been reclaimed.
        });
        return;
      }
      setSessions((current) => [...current, session]);
      setVisibleIds((current) => {
        if (mode === 'split') {
          return current.length > 0 ? [current[0], session.id] : [session.id];
        }
        if (current.length > 1) {
          const next = [...current];
          next[paneIndex] = session.id;
          return next;
        }
        return [session.id];
      });
    } catch (cause: unknown) {
      if (requestIsCurrent(requestJobId, generation)) {
        setError(cause instanceof Error ? cause.message : 'Could not create terminal');
      }
    } finally {
      if (requestIsCurrent(requestJobId, generation)) setBusy(false);
    }
  }

  async function kill(terminalId: string) {
    if (busy) return;
    const requestJobId = jobId;
    const generation = jobGenerationRef.current;
    setBusy(true);
    setError(null);
    try {
      await deleteAgentTerminal(requestJobId, terminalId);
      if (!requestIsCurrent(requestJobId, generation)) return;
      const remaining = sessions.filter((session) => session.id !== terminalId);
      const nextVisible = visibleIds.filter((id) => id !== terminalId);
      if (nextVisible.length === 0 && remaining.length > 0) nextVisible.push(remaining[0].id);
      setSessions(remaining);
      setVisibleIds(nextVisible);
    } catch (cause: unknown) {
      if (requestIsCurrent(requestJobId, generation)) {
        setError(cause instanceof Error ? cause.message : 'Could not kill terminal');
      }
    } finally {
      if (requestIsCurrent(requestJobId, generation)) setBusy(false);
    }
  }

  function select(paneIndex: number, terminalId: string) {
    setVisibleIds((current) => {
      const next = [...current];
      const otherPane = paneIndex === 0 ? 1 : 0;
      if (next[otherPane] === terminalId) {
        next[otherPane] = next[paneIndex];
      }
      next[paneIndex] = terminalId;
      return next;
    });
  }

  return (
    <section aria-label="Workspace terminal" className="flex h-full min-h-[28rem] flex-col">
      {error ? (
        <p
          role="alert"
          className="mb-2 shrink-0 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600"
        >
          {error}
        </p>
      ) : null}
      {visible.length ? (
        <div
          className={`grid min-h-0 flex-1 gap-2 ${
            visible.length === 2
              ? 'grid-rows-2 lg:grid-cols-2 lg:grid-rows-1'
              : 'grid-cols-1 grid-rows-1'
          }`}
        >
          {visible.map((terminal, paneIndex) => (
            <TerminalPane
              key={`${paneIndex}-${terminal.id}`}
              jobId={jobId}
              terminal={terminal}
              sessions={sessions}
              paneIndex={paneIndex}
              ready={ready}
              canCreate={canCreate}
              canSplit={canCreate && visible.length < 2}
              busy={busy}
              onSelect={(terminalId) => select(paneIndex, terminalId)}
              onNew={() => void create('new', paneIndex)}
              onSplit={() => void create('split')}
              onKill={() => void kill(terminal.id)}
            />
          ))}
        </div>
      ) : (
        <div className="flex min-h-80 flex-1 flex-col items-center justify-center rounded-xl border border-gray-200 bg-white px-6 text-center shadow-sm">
          <span className="mb-3 rounded-lg border border-gray-200 p-2 text-gray-500">
            <svg aria-hidden="true" className="h-5 w-5" fill="none" viewBox="0 0 24 24">
              <rect
                x="3.5"
                y="4"
                width="17"
                height="16"
                rx="2"
                stroke="currentColor"
                strokeWidth="1.6"
              />
              <path
                d="m7 9 3 3-3 3m5 0h4"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.6"
              />
            </svg>
          </span>
          <p className="text-sm font-medium text-gray-800">
            {loading || awaitingInitialLoad ? 'Loading terminals…' : 'No open terminals'}
          </p>
          <p className="mt-1 max-w-sm text-xs leading-5 text-gray-500">
            Terminal changes appear in this workspace&apos;s Git and Files views, but do not
            automatically update an existing pull request.
          </p>
          {loading || awaitingInitialLoad ? null : loadFailed ? (
            <button
              type="button"
              onClick={retryLoad}
              className="mt-4 inline-flex items-center rounded-md border border-gray-200 bg-white px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
            >
              Retry
            </button>
          ) : (
            <button
              type="button"
              aria-label="New terminal"
              onClick={() => void create('new')}
              disabled={!canCreate || busy}
              className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-gray-900 px-3 py-2 text-xs font-medium text-white hover:bg-gray-800 disabled:opacity-50"
            >
              <span aria-hidden="true" className="text-base leading-none">
                +
              </span>
              {busy ? 'Starting…' : 'New terminal'}
            </button>
          )}
        </div>
      )}
      {visible.length ? (
        <p className="mt-2 shrink-0 px-1 text-[10px] leading-4 text-gray-400">
          Changes appear in Git and Files, but do not automatically update an existing pull request.
        </p>
      ) : null}
    </section>
  );
}
