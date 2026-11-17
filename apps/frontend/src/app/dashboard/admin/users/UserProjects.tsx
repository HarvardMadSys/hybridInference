'use client';

import { useEffect, useState } from 'react';
import { getUserProjects, type AdminUserProjects } from '@/lib/api/admin';
import type { ApiKeyListItem, Project } from '@/lib/api/user';
import { getErrorMessage } from '@/lib/utils/errors';
import { InlineErrorText } from '@/components/ui/InlineErrorText';

function usd(value: number): string {
  return value < 0.01 && value > 0 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

function spend(spent: number, budget: number | null | undefined, window: string): string {
  return budget == null ? `${usd(spent)} ${window}` : `${usd(spent)} of ${usd(budget)} ${window}`;
}

/** What a project limits beyond its spend, in a few words. */
function projectLimits(project: Project): string[] {
  const limits: string[] = [];
  if (project.allowed_models != null) limits.push(`${project.allowed_models.length} model(s)`);
  if (project.max_concurrent_requests != null) {
    limits.push(`up to ${project.max_concurrent_requests} at once`);
  }
  return limits;
}

function KeyLine({ apiKey }: { apiKey: ApiKeyListItem }) {
  const limits = [
    apiKey.budget_daily_usd != null ? `daily ${usd(apiKey.budget_daily_usd)}` : null,
    apiKey.budget_monthly_usd != null ? `monthly ${usd(apiKey.budget_monthly_usd)}` : null,
    apiKey.allowed_models != null ? `${apiKey.allowed_models.length} model(s)` : null,
  ].filter(Boolean);
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-x-3 py-1 text-[12px]">
      <span className="min-w-0 truncate">
        <span className={apiKey.name ? 'font-medium text-gray-900' : 'italic text-gray-400'}>
          {apiKey.name ?? 'Unnamed key'}
        </span>{' '}
        <code className="text-gray-500">{apiKey.key_masked}</code>
        {apiKey.status !== 'active' && (
          <span className="ml-1.5 text-[11px] text-gray-400">{apiKey.status}</span>
        )}
      </span>
      <span className="tabular-nums text-gray-500">
        {usd(apiKey.spent_today_usd ?? 0)} today
        {limits.length > 0 && ` · ${limits.join(' · ')}`}
      </span>
    </li>
  );
}

function KeyList({ keys }: { keys: ApiKeyListItem[] }) {
  if (keys.length === 0) return <p className="mt-1 pl-3 text-[12px] text-gray-400">No keys.</p>;
  return (
    <ul className="mt-1 divide-y divide-gray-100 border-l-2 border-gray-100 pl-3">
      {keys.map((key) => (
        <KeyLine key={key.key_prefix} apiKey={key} />
      ))}
    </ul>
  );
}

/**
 * The account's projects and the keys in each, read-only. An administrator
 * reads them to explain a `scope=project` refusal or a spend; the owner
 * manages them, and the account quota is set in the account settings above.
 */
export function UserProjects({ userId }: { userId: string }) {
  const [data, setData] = useState<AdminUserProjects | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await getUserProjects(userId);
        if (!cancelled) setData(result);
      } catch (err) {
        if (!cancelled) setError(getErrorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const projectIds = new Set((data?.projects ?? []).map((project) => project.id));
  const archivedKeys = (data?.keys ?? []).filter((key) => !projectIds.has(key.project_id ?? ''));

  return (
    <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
      <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
        Projects and keys
      </div>
      {error && <InlineErrorText message={error} />}
      {!data && !error && <p className="text-[12px] text-gray-400">Loading…</p>}
      {data?.projects.map((project) => {
        const limits = projectLimits(project);
        return (
          <section key={project.id} aria-label={project.name} className="pt-1">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-[13px]">
              <span className="font-semibold text-gray-900">
                {project.name}
                {project.is_default && project.name !== 'Default' && (
                  <span className="ml-1.5 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
                    Default
                  </span>
                )}
              </span>
              <span className="text-[12px] tabular-nums text-gray-500">
                {spend(project.spent_today_usd, project.budget_daily_usd, 'today')}
                {' · '}
                {spend(project.spent_month_usd, project.budget_monthly_usd, 'this month')}
                {limits.length > 0 && ` · ${limits.join(' · ')}`}
              </span>
            </div>
            <KeyList keys={data.keys.filter((key) => key.project_id === project.id)} />
          </section>
        );
      })}
      {archivedKeys.length > 0 && (
        <section aria-label="Archived projects" className="pt-1">
          <div className="text-[13px] font-semibold text-gray-500">Archived projects</div>
          <KeyList keys={archivedKeys} />
        </section>
      )}
    </div>
  );
}
