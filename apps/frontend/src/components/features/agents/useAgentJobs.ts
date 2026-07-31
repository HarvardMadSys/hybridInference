'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getAgentJob,
  getAgentJobArtifact,
  getAgentJobFiles,
  getAgentJobThread,
  listAgentJobEvents,
  listAgentJobs,
  listAgentProjects,
  streamAgentJob,
} from '@/lib/api/agents';
import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentJobFileApi,
  AgentJobFileEntryApi,
  AgentJobSymlinkApi,
  AgentProjectApi,
  AgentThreadApi,
} from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import type { AgentJob } from './types';

const ACTIVE_ATTEMPT_PHASES = new Set([
  'started',
  'checked_out',
  'context_restored',
  'setup',
  'workspace_preparing',
  'workspace_ready',
  'workspace_finalizing',
]);

/** Apply one live lifecycle event to the cached owner-facing job row. */
export function applyAgentLifecycleEvent(
  current: AgentJobApi,
  event: AgentJobEventApi,
): AgentJobApi {
  if (event.event_type !== 'lifecycle') return current;
  const phase = event.payload?.phase;
  if (typeof phase !== 'string') return current;
  if (ACTIVE_ATTEMPT_PHASES.has(phase)) {
    return { ...current, state: 'running', current_attempt_id: event.attempt_id };
  }
  if (phase === 'publishing') {
    return { ...current, state: 'publishing', current_attempt_id: event.attempt_id };
  }
  return current;
}

// Data hooks for the /agents surface.
//
// The API client and the adapter were both complete and tested while the UI
// still rendered fixtures, so this is the wiring between them rather than new
// behaviour. Two things it deliberately does not do: invent values the API does
// not return (the adapter decides that), and treat the stream ending as an
// error — the server's SSE cap is finite by design, so a drop is the expected
// shape and the client resumes from the last event id.

/** List the caller's jobs, refreshed on an interval while any are live. */
export function useAgentJobList(
  pollMs = 10_000,
  archived = false,
): {
  jobs: AgentJob[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [jobs, setJobs] = useState<AgentJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    listAgentJobs(50, archived)
      .then((api: AgentJobApi[]) => {
        if (cancelled) return;
        setJobs(api.map((job) => toDisplayJob(job)));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : 'could not load jobs');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [archived, tick]);

  // Only poll while something can still change; a list of finished jobs does
  // not need a request every ten seconds.
  const hasLive = jobs.some((job) => job.state === 'running' || job.state === 'queued');
  useEffect(() => {
    if (!hasLive) return undefined;
    const timer = setInterval(reload, pollMs);
    return () => clearInterval(timer);
  }, [hasLive, pollMs, reload]);

  return { jobs, loading, error, reload };
}

/** The caller's projects, for the sidebar's folder tree. */
export function useAgentProjects(archived = false): {
  projects: AgentProjectApi[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [projects, setProjects] = useState<AgentProjectApi[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    listAgentProjects(archived)
      .then((api) => {
        if (cancelled) return;
        setProjects(api);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled)
          setError(cause instanceof Error ? cause.message : 'could not load projects');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [archived, tick]);

  return { projects, loading, error, reload };
}

/**
 * One project's jobs, fetched on demand.
 *
 * The sidebar's shared job list is a global newest-first page, so a project
 * that has been quiet holds only its most recent turns there. Expanding such a
 * folder pulls that project's own page instead of widening the global one.
 */
export function useProjectJobs(limit = 200): {
  jobsByRepo: Map<string, AgentJob[]>;
  loadingRepos: Set<string>;
  errorRepos: Map<string, string>;
  load: (repo: string) => void;
} {
  const [jobsByRepo, setJobsByRepo] = useState<Map<string, AgentJob[]>>(new Map());
  const [loadingRepos, setLoadingRepos] = useState<Set<string>>(new Set());
  const [errorRepos, setErrorRepos] = useState<Map<string, string>>(new Map());
  const inFlight = useRef<Set<string>>(new Set());

  const load = useCallback(
    (repo: string) => {
      if (inFlight.current.has(repo)) return;
      inFlight.current.add(repo);
      setLoadingRepos((current) => new Set(current).add(repo));
      listAgentJobs(limit, false, repo)
        .then((api: AgentJobApi[]) => {
          setJobsByRepo((current) =>
            new Map(current).set(
              repo,
              api.map((job) => toDisplayJob(job)),
            ),
          );
          setErrorRepos((current) => {
            if (!current.has(repo)) return current;
            const next = new Map(current);
            next.delete(repo);
            return next;
          });
        })
        .catch((cause: unknown) => {
          setErrorRepos((current) =>
            new Map(current).set(
              repo,
              cause instanceof Error ? cause.message : 'could not load this project',
            ),
          );
        })
        .finally(() => {
          inFlight.current.delete(repo);
          setLoadingRepos((current) => {
            const next = new Set(current);
            next.delete(repo);
            return next;
          });
        });
    },
    [limit],
  );

  return { jobsByRepo, loadingRepos, errorRepos, load };
}

/** One job, its event log, and its patch — kept live over SSE. */
export function useAgentJob(jobId: string): {
  job: AgentJob | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [api, setApi] = useState<AgentJobApi | null>(null);
  const [events, setEvents] = useState<AgentJobEventApi[]>([]);
  const [patch, setPatch] = useState<string | null>(null);
  const [thread, setThread] = useState<AgentThreadApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const seen = useRef<Set<number>>(new Set());

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    seen.current = new Set();
    setPatch(null);
    setLoading(true);

    Promise.all([getAgentJob(jobId), listAgentJobEvents(jobId), getAgentJobThread(jobId)])
      .then(([job, page, foundThread]) => {
        if (cancelled) return;
        setApi(job);
        setEvents(page.events);
        setThread(foundThread);
        page.events.forEach((event) => seen.current.add(event.id));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : 'could not load the job');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [jobId, tick]);

  // The patch only exists once the job produced one; a 404 means "this job
  // changed nothing", which is a real outcome rather than a failure.
  const state = api?.state;
  useEffect(() => {
    if (!jobId || !state) return undefined;
    let cancelled = false;
    getAgentJobArtifact(jobId, 'patch')
      .then((artifact) => {
        if (!cancelled) setPatch(artifact?.content ?? null);
      })
      .catch(() => {
        if (!cancelled) setPatch(null);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, state]);

  // Live events. Terminal jobs have nothing more to say, so no stream is
  // opened for them at all.
  const isLive =
    state === 'waiting' || state === 'running' || state === 'queued' || state === 'publishing';
  useEffect(() => {
    if (!jobId || !isLive) return undefined;
    const controller = new AbortController();
    void streamAgentJob(jobId, {
      signal: controller.signal,
      onEvent: (event) => {
        // The stream can replay across a reconnect, so dedupe on the global id
        // the server assigns rather than assuming each frame is new.
        if (seen.current.has(event.id)) return;
        seen.current.add(event.id);
        setEvents((current) => [...current, event]);
        // The stream is also the fastest authority for a waiting/queued child
        // becoming active; keep the status pill in step without polling.
        if (event.event_type === 'lifecycle') {
          setApi((current) => (current ? applyAgentLifecycleEvent(current, event) : current));
        }
      },
      onFinished: () => reload(),
    }).catch(() => {
      // A dropped stream is the expected shape when the server's cap fires;
      // the reload picks up anything missed.
      if (!controller.signal.aborted) reload();
    });
    return () => controller.abort();
  }, [jobId, isLive, reload]);

  const job = api ? toDisplayJob(api, { events, patch, thread }) : null;
  return { job, loading, error, reload };
}

/** One directory of the workspace tree, as far as it has been loaded. */
export interface WorkspaceDirectory {
  entries: AgentJobFileEntryApi[] | null;
  loading: boolean;
  error: string | null;
}

export interface WorkspaceFilesState {
  /** Loaded directories keyed by path; the workspace root is the empty string. */
  directories: Record<string, WorkspaceDirectory>;
  expanded: Record<string, boolean>;
  selected: string | null;
  file: AgentJobFileApi | AgentJobSymlinkApi | null;
  fileLoading: boolean;
  fileError: string | null;
  source: 'workspace' | 'snapshot' | null;
  toggleDirectory: (path: string) => void;
  openFile: (path: string) => void;
  closeFile: () => void;
  reload: () => void;
}

const filesError = (cause: unknown): string =>
  cause instanceof Error ? cause.message : 'Could not load workspace files';

/**
 * Browse the workspace as a lazily expanded tree, only while the tab is active.
 *
 * Each directory is one request, cached under its path, so expanding a folder
 * never discards the rest of the tree. A refresh (job state change, or a save)
 * refetches the root, every open folder and the open file — nothing else.
 */
export function useWorkspaceFiles(
  jobId: string,
  enabled: boolean,
  refreshKey = '',
): WorkspaceFilesState {
  const [directories, setDirectories] = useState<Record<string, WorkspaceDirectory>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<AgentJobFileApi | AgentJobSymlinkApi | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [source, setSource] = useState<'workspace' | 'snapshot' | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((value) => value + 1), []);

  // A refresh invalidates every in-flight request: results from an older
  // generation belong to a workspace state the user is no longer looking at.
  const generation = useRef(0);
  const alive = useRef(true);
  const expandedRef = useRef(expanded);
  const selectedRef = useRef(selected);

  useEffect(() => {
    expandedRef.current = expanded;
    selectedRef.current = selected;
  }, [expanded, selected]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const loadDirectory = useCallback(
    async (path: string) => {
      const mine = generation.current;
      setDirectories((current) => ({
        ...current,
        [path]: { entries: current[path]?.entries ?? null, loading: true, error: null },
      }));
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!alive.current || mine !== generation.current) return;
        setDirectories((current) => ({
          ...current,
          [path]: {
            entries: node.kind === 'directory' ? (node.entries ?? []) : [],
            loading: false,
            error: null,
          },
        }));
        if (path === '') setSource(node.source ?? null);
      } catch (cause) {
        if (!alive.current || mine !== generation.current) return;
        setDirectories((current) => ({
          ...current,
          [path]: { entries: null, loading: false, error: filesError(cause) },
        }));
      }
    },
    [jobId],
  );

  const loadFile = useCallback(
    async (path: string) => {
      const mine = generation.current;
      setFileLoading(true);
      setFileError(null);
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!alive.current || mine !== generation.current) return;
        if (node.kind === 'directory') {
          // The entry turned out to be a directory — show it in the tree.
          setDirectories((current) => ({
            ...current,
            [path]: { entries: node.entries ?? [], loading: false, error: null },
          }));
          setExpanded((current) => ({ ...current, [path]: true }));
          setSelected(null);
          setFile(null);
        } else {
          setFile(node);
        }
      } catch (cause) {
        if (!alive.current || mine !== generation.current) return;
        setFile(null);
        setFileError(filesError(cause));
      } finally {
        if (alive.current && mine === generation.current) setFileLoading(false);
      }
    },
    [jobId],
  );

  useEffect(() => {
    if (!enabled || !jobId) return;
    generation.current += 1;
    setDirectories({});
    const open = expandedRef.current;
    for (const path of ['', ...Object.keys(open).filter((path) => open[path])]) {
      void loadDirectory(path);
    }
    if (selectedRef.current) void loadFile(selectedRef.current);
  }, [enabled, jobId, refreshKey, tick, loadDirectory, loadFile]);

  const toggleDirectory = useCallback(
    (path: string) => {
      const open = Boolean(expanded[path]);
      setExpanded((current) => ({ ...current, [path]: !open }));
      if (!open && !directories[path]?.entries) void loadDirectory(path);
    },
    [directories, expanded, loadDirectory],
  );

  const openFile = useCallback(
    (path: string) => {
      setSelected(path);
      setFile(null);
      void loadFile(path);
    },
    [loadFile],
  );

  const closeFile = useCallback(() => {
    setSelected(null);
    setFile(null);
    setFileError(null);
  }, []);

  return {
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
  };
}
