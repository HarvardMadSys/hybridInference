'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth } from '@/components/providers';
import { archiveAgentJob } from '@/lib/api/agents';
import { groupJobsByProject, projectLabel } from './conversations';
import type { ConversationRow, ProjectSection } from './conversations';
import { PaneResizer } from './PaneResizer';
import { useAgentJobList, useAgentProjects, useProjectJobs } from './useAgentJobs';
import { announcePaneResize, useResizablePane } from './useResizablePane';
import type { AgentJob, AgentJobState } from './types';

export { groupJobsByConversation, groupJobsByProject } from './conversations';

export const SIDEBAR_WIDTH_STORAGE_KEY = 'agents.sidebar.width';
export const SIDEBAR_PROJECTS_STORAGE_KEY = 'agents.sidebar.projects';
const SIDEBAR_DEFAULT_WIDTH = 288; // matches the previous fixed w-72
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 520;
/** Conversations shown per project before "Show more". */
const PROJECT_PAGE_SIZE = 15;

// Sidebar status dots stay deliberately minimal (Codex-style titles-only
// rows), but unlike Codex our jobs burn budget and can be held by publish
// gates, so "which job is running / held" must be visible without opening it.
const DOT_CLASS: Record<AgentJobState, string> = {
  running: 'bg-blue-500 animate-pulse',
  needs_review: 'bg-amber-400',
  done: 'bg-emerald-500',
  failed: 'bg-red-400',
  cancelled: 'bg-gray-300',
  queued: 'border border-gray-300 bg-transparent',
};

function initialsOf(name: string | null | undefined, email: string | null | undefined): string {
  const source = name || email || '?';
  return source.slice(0, 2).toUpperCase();
}

/** Which folders the user has explicitly opened or closed, by repo. */
function readExpandedOverrides(): Record<string, boolean> {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(SIDEBAR_PROJECTS_STORAGE_KEY);
  } catch {
    return {};
  }
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const entries = Object.entries(parsed as Record<string, unknown>).filter(
      ([, value]) => typeof value === 'boolean',
    );
    return Object.fromEntries(entries) as Record<string, boolean>;
  } catch {
    return {};
  }
}

export function AgentsSidebar({ collapsed = false }: { collapsed?: boolean }) {
  const pathname = usePathname() ?? '';
  const router = useRouter();
  const { state } = useAuth();
  const displayName = state.user?.user_name || state.user?.email || '';
  const [hiddenThreads, setHiddenThreads] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({});
  const [shownPerRepo, setShownPerRepo] = useState<Record<string, number>>({});
  const pane = useResizablePane({
    storageKey: SIDEBAR_WIDTH_STORAGE_KEY,
    defaultWidth: SIDEBAR_DEFAULT_WIDTH,
    minWidth: SIDEBAR_MIN_WIDTH,
    maxWidth: SIDEBAR_MAX_WIDTH,
    side: 'start',
    maxViewportFraction: 0.4,
  });

  const { jobs, loading, error, reload } = useAgentJobList();
  const { projects, loading: projectsLoading, reload: reloadProjects } = useAgentProjects();
  const { jobsByRepo, loadingRepos, errorRepos, load: loadRepo } = useProjectJobs();

  // Restore after mount, not during render: the server has no storage, so
  // seeding initial state from it would be a hydration mismatch.
  useEffect(() => setExpandedOverrides(readExpandedOverrides()), []);

  // Collapsing hands this width to the panes on the right, which size themselves
  // against what is left of the shell.
  useEffect(() => {
    announcePaneResize();
  }, [collapsed]);

  // The list polls only while something is live, so a conversation created by
  // navigation — a new task, a fork — would otherwise not appear until a
  // manual refresh. Reload on route change; the mount fetch already ran.
  const mountPath = useRef(true);
  useEffect(() => {
    if (mountPath.current) {
      mountPath.current = false;
      return;
    }
    reload();
    reloadProjects();
  }, [pathname, reload, reloadProjects]);

  // A project's own page supersedes nothing — it is merged with the shared
  // newest-first page, which is the one that keeps polling, so live state from
  // the poll wins wherever the two overlap.
  const visibleJobs = useMemo(() => {
    const byId = new Map<string, AgentJob>();
    for (const list of jobsByRepo.values()) for (const job of list) byId.set(job.id, job);
    for (const job of jobs) byId.set(job.id, job);
    return [...byId.values()].filter((job) => !hiddenThreads.has(job.threadId ?? job.id));
  }, [jobs, jobsByRepo, hiddenThreads]);

  const projectByRepo = useMemo(
    () => new Map(projects.map((project) => [project.repo, project])),
    [projects],
  );

  // The summary decides which folders exist and in what order. A repo the job
  // page knows about but the summary does not — a project created since the
  // last summary fetch — is newer than all of them, so it goes in front. When
  // the summary is unavailable the tree degrades to what the jobs imply.
  const folders = useMemo<ProjectSection[]>(() => {
    const byRepo = new Map(groupJobsByProject(visibleJobs).map((entry) => [entry.repo, entry]));
    const summarized = projects.map((project) => {
      const section = byRepo.get(project.repo);
      byRepo.delete(project.repo);
      return (
        section ?? { repo: project.repo, label: projectLabel(project.repo), conversations: [] }
      );
    });
    return [...byRepo.values(), ...summarized];
  }, [projects, visibleJobs]);

  const activeRepo = visibleJobs.find((job) => pathname === `/agents/${job.id}`)?.repo ?? null;
  // With nothing open, the most recent project is the one worth showing.
  const defaultRepo = activeRepo ?? folders[0]?.repo ?? null;
  const isExpanded = useCallback(
    (repo: string) => expandedOverrides[repo] ?? repo === defaultRepo,
    [expandedOverrides, defaultRepo],
  );

  // An expanded folder holding fewer conversations than the project has must
  // pull its own page. Keyed on the count so it refetches when the project
  // grows, rather than on every poll.
  const requested = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const folder of folders) {
      if (!isExpanded(folder.repo)) continue;
      const summary = projectByRepo.get(folder.repo);
      if (!summary || summary.task_count <= folder.conversations.length) continue;
      const key = `${folder.repo}:${summary.task_count}`;
      if (requested.current.has(key)) continue;
      requested.current.add(key);
      loadRepo(folder.repo);
    }
  }, [folders, isExpanded, projectByRepo, loadRepo]);

  function toggleProject(repo: string) {
    setExpandedOverrides((current) => {
      const next = { ...current, [repo]: !isExpanded(repo) };
      try {
        window.localStorage.setItem(SIDEBAR_PROJECTS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        // Blocked storage only forgets the tree's shape, never breaks it.
      }
      return next;
    });
  }

  async function archiveConversation(key: string, jobId: string, jobIds: string[]) {
    setArchiving(key);
    setArchiveError(null);
    try {
      await archiveAgentJob(jobId);
      setHiddenThreads((current) => new Set(current).add(key));
      reload();
      reloadProjects();
      if (jobIds.some((id) => pathname === `/agents/${id}`)) router.replace('/agents');
    } catch (cause: unknown) {
      setArchiveError(cause instanceof Error ? cause.message : 'Could not archive the task.');
    } finally {
      setArchiving(null);
    }
  }

  if (collapsed) return null;

  const busy = loading || projectsLoading;

  return (
    <>
      <aside
        id="agents-sidebar"
        style={{ width: pane.width }}
        className="flex shrink-0 flex-col bg-gray-50"
      >
        <div className="flex-1 overflow-y-auto px-3 py-3">
          <Link
            href="/agents"
            className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-200/60 ${
              pathname === '/agents' ? 'bg-gray-200/80 text-gray-900' : ''
            }`}
          >
            <svg
              className="h-4 w-4 text-gray-500"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z"
              />
            </svg>
            New task
          </Link>

          {busy && folders.length === 0 ? (
            <p className="mt-2 px-2 text-[13px] text-gray-400">Loading…</p>
          ) : null}
          {error ? (
            <p className="mt-2 px-2 text-[13px] text-red-600" role="alert">
              {error}
            </p>
          ) : null}
          {archiveError ? (
            <p className="mt-2 px-2 text-[13px] text-red-600" role="alert">
              {archiveError}
            </p>
          ) : null}
          {!busy && !error && !archiveError && folders.length === 0 ? (
            <p className="mt-2 px-2 text-[13px] text-gray-400">No jobs yet.</p>
          ) : null}

          {folders.map((folder) => (
            <ProjectFolder
              key={folder.repo}
              section={folder}
              expanded={isExpanded(folder.repo)}
              onToggle={() => toggleProject(folder.repo)}
              activeCount={projectByRepo.get(folder.repo)?.active_count ?? 0}
              shown={shownPerRepo[folder.repo] ?? PROJECT_PAGE_SIZE}
              onShowMore={() =>
                setShownPerRepo((current) => ({
                  ...current,
                  [folder.repo]: (current[folder.repo] ?? PROJECT_PAGE_SIZE) + PROJECT_PAGE_SIZE,
                }))
              }
              loading={loadingRepos.has(folder.repo)}
              error={errorRepos.get(folder.repo) ?? null}
              pathname={pathname}
              archiving={archiving}
              onArchive={archiveConversation}
            />
          ))}

          <Link
            href="/agents/archived"
            className={`mt-3 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-gray-200/60 ${
              pathname === '/agents/archived' ? 'bg-gray-200/80 text-gray-900' : 'text-gray-600'
            }`}
          >
            <svg
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              aria-hidden="true"
            >
              <path
                d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Archived
          </Link>

          <Link
            href="/agents/integrations"
            className={`mt-1 flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-gray-200/60 ${
              pathname === '/agents/integrations' ? 'bg-gray-200/80 text-gray-900' : 'text-gray-600'
            }`}
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M8 5v3m8-3v3M6.5 8h11v2.5a5.5 5.5 0 0 1-11 0V8ZM12 16v3"
              />
            </svg>
            Integrations
          </Link>
        </div>

        <div className="flex shrink-0 items-center gap-2.5 border-t border-gray-200 px-4 py-3">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-crimson/10 text-[11px] font-semibold text-crimson">
            {initialsOf(state.user?.user_name, state.user?.email)}
          </span>
          <span className="truncate text-[13px] font-medium text-gray-800">{displayName}</span>
        </div>
      </aside>
      <PaneResizer pane={pane} label="Resize task list" controls="agents-sidebar" />
    </>
  );
}

function ProjectFolder({
  section,
  expanded,
  onToggle,
  activeCount,
  shown,
  onShowMore,
  loading,
  error,
  pathname,
  archiving,
  onArchive,
}: {
  section: ProjectSection;
  expanded: boolean;
  onToggle: () => void;
  activeCount: number;
  shown: number;
  onShowMore: () => void;
  loading: boolean;
  error: string | null;
  pathname: string;
  archiving: string | null;
  onArchive: (key: string, jobId: string, jobIds: string[]) => void;
}) {
  const listId = `agents-project-${section.repo.replace(/[^A-Za-z0-9]/g, '-')}`;
  const visible = section.conversations.slice(0, shown);
  const hidden = section.conversations.length - visible.length;

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={listId}
        title={section.repo}
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[13px] font-medium text-gray-700 hover:bg-gray-200/60"
      >
        <svg
          className={`h-3 w-3 shrink-0 text-gray-400 transition-transform ${
            expanded ? 'rotate-90' : ''
          }`}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.4}
          aria-hidden="true"
        >
          <path d="m9 6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <svg
          className="h-4 w-4 shrink-0 text-gray-500"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.8}
          aria-hidden="true"
        >
          <path
            d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17V7.5Z"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="min-w-0 flex-1 truncate">{section.label}</span>
        {/* A folded folder must still say it is hiding live work — the same
            reason the rows carry status dots at all. */}
        {!expanded && activeCount > 0 ? (
          <span
            className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-blue-500"
            aria-label={`${activeCount} running or queued`}
          />
        ) : null}
      </button>

      {expanded ? (
        <div id={listId} className="mt-0.5 space-y-0.5">
          {visible.map(({ key, job, jobIds }: ConversationRow) => {
            const href = `/agents/${job.id}`;
            const isActive = jobIds.some((id) => pathname === `/agents/${id}`);
            return (
              <div key={key} className="group relative">
                <Link
                  href={href}
                  title={`${job.repo} · ${job.title}`}
                  className={`flex w-full items-center gap-2 rounded-md py-1.5 pl-4 pr-9 text-left text-[13px] ${
                    isActive
                      ? 'bg-gray-200/80 font-medium text-gray-900'
                      : 'text-gray-600 hover:bg-gray-200/60'
                  }`}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[job.state]}`} />
                  <span className="min-w-0 flex-1 truncate">{job.title}</span>
                </Link>
                <button
                  type="button"
                  aria-label={`Archive ${job.title}`}
                  title="Archive task"
                  disabled={archiving === key}
                  onClick={() => onArchive(key, job.id, jobIds)}
                  className="absolute right-1 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-gray-500 opacity-0 transition hover:bg-gray-300/70 hover:text-gray-800 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-crimson/30 disabled:cursor-wait disabled:opacity-60 group-hover:opacity-100 group-focus-within:opacity-100"
                >
                  <svg
                    className="h-4 w-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.8}
                    aria-hidden="true"
                  >
                    <path
                      d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>
            );
          })}

          {loading && visible.length === 0 ? (
            <p className="px-4 py-1 text-[12px] text-gray-400">Loading…</p>
          ) : null}
          {error ? (
            <p className="px-4 py-1 text-[12px] text-red-600" role="alert">
              {error}
            </p>
          ) : null}
          {hidden > 0 ? (
            <button
              type="button"
              onClick={onShowMore}
              className="w-full rounded-md px-4 py-1 text-left text-[12px] text-gray-400 hover:bg-gray-200/60 hover:text-gray-600"
            >
              Show {hidden} more
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
