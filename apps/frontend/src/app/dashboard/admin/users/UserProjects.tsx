'use client';

import { useEffect, useState } from 'react';
import {
  ProjectsAndKeys,
  type KeyEntry,
} from '@/components/features/console-shared/ProjectsAndKeys';
import { getUserProjects, type AdminUserProjects } from '@/lib/api/admin';
import type { ApiKeyListItem, Project } from '@/lib/api/user';
import { getErrorMessage } from '@/lib/utils/errors';
import { useMoney } from '@/lib/hooks/useMoney';

type Money = ReturnType<typeof useMoney>;

/**
 * One of a project's or key's money fields in its own ledger currency: the
 * native amount with the record's `currency` when it has them, else USD.
 * Cents, or four places below one cent.
 */
function amount(money: Money, source: object, field: string): string {
  const value = money.amount(source, field) ?? 0;
  const digits = value < 0.01 && value > 0 ? 4 : 2;
  return money.format(
    value,
    { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: false },
    money.sourceCurrency(source),
  );
}

function spend(money: Money, project: Project, field: 'today' | 'month', window: string): string {
  const spent = amount(money, project, `spent_${field}`);
  const budgetField = field === 'today' ? 'budget_daily' : 'budget_monthly';
  return money.amount(project, budgetField) == null
    ? `${spent} ${window}`
    : `${spent} of ${amount(money, project, budgetField)} ${window}`;
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

/** A key as the administrator reads it: its name, its masked value, today's spend and its limits. */
function keyEntry(money: Money, apiKey: ApiKeyListItem): KeyEntry {
  const limits = [
    money.amount(apiKey, 'budget_daily') != null
      ? `daily ${amount(money, apiKey, 'budget_daily')}`
      : null,
    money.amount(apiKey, 'budget_monthly') != null
      ? `monthly ${amount(money, apiKey, 'budget_monthly')}`
      : null,
    apiKey.allowed_models != null ? `${apiKey.allowed_models.length} model(s)` : null,
  ].filter(Boolean);
  return {
    id: apiKey.key_prefix,
    name: apiKey.name ?? null,
    masked: apiKey.key_masked,
    statusNote: apiKey.status !== 'active' ? apiKey.status : undefined,
    figures: `${amount(money, apiKey, 'spent_today')} today${
      limits.length > 0 ? ` · ${limits.join(' · ')}` : ''
    }`,
  };
}

/**
 * The account's projects and the keys in each, read-only. An administrator
 * reads them to explain a `scope=project` refusal or a spend; the owner
 * manages them, and the account quota is set in the account settings above.
 */
export function UserProjects({ userId }: { userId: string }) {
  const money = useMoney();
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
    <ProjectsAndKeys
      projects={
        data
          ? data.projects.map((project) => {
              const limits = projectLimits(project);
              return {
                id: project.id,
                name: project.name,
                isDefault: project.is_default,
                figures: `${spend(money, project, 'today', 'today')} · ${spend(
                  money,
                  project,
                  'month',
                  'this month',
                )}${limits.length > 0 ? ` · ${limits.join(' · ')}` : ''}`,
                keys: data.keys
                  .filter((key) => key.project_id === project.id)
                  .map((key) => keyEntry(money, key)),
              };
            })
          : null
      }
      archivedKeys={archivedKeys.map((key) => keyEntry(money, key))}
      error={error}
    />
  );
}
