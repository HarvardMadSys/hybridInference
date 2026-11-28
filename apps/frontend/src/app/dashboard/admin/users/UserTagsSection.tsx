'use client';

import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  addUserTagMembers,
  removeUserTagMember,
  type UserTagLimits,
  type UserTagMembership,
} from '@/lib/api/admin';
import type { ConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { Select } from '@/components/ui/Select';
import { useMoney } from '@/lib/hooks/useMoney';
import { TagChip } from './TagChip';
import { USER_TAGS_QUERY_KEY, useUserTags } from './hooks/useUserTags';
import { USERS_LIST_QUERY_KEY } from './hooks/useUsers';
import { BUTTON_SECONDARY, FIELD_LABEL, SECTION } from './ui';

/** What the account's applied tags hold it to, in one line; empty when nothing. */
export function tagLimitsSummary(
  limits: UserTagLimits | undefined,
  format: (value: number) => string,
): string {
  if (!limits) return '';
  const parts: string[] = [];
  if (limits.quota_daily_cost != null) {
    parts.push(`daily quota ≤ ${format(Number(limits.quota_daily_cost))}`);
  }
  if (limits.quota_monthly_cost != null) {
    parts.push(`monthly quota ≤ ${format(Number(limits.quota_monthly_cost))}`);
  }
  if (limits.max_concurrent_requests != null) {
    const cap = limits.max_concurrent_requests;
    parts.push(`≤ ${cap} concurrent request${cap === 1 ? '' : 's'}`);
  }
  if (limits.allowed_models != null) {
    parts.push(
      limits.allowed_models.length === 0
        ? 'no model (the tags allow none in common)'
        : `only ${limits.allowed_models.join(', ')}`,
    );
  }
  return parts.join(' · ');
}

/**
 * An account's user tags, in its detail view: the tags it carries and how,
 * the limits they hold it to, and adding or taking one away.
 */
export function UserTagsSection({
  userId,
  memberships,
  limits,
  confirmation,
  onChanged,
}: {
  userId: string;
  memberships: UserTagMembership[];
  limits: UserTagLimits | undefined;
  confirmation: ConfirmAction;
  /** Re-read the account after a change. */
  onChanged: () => Promise<void>;
}) {
  const money = useMoney();
  const queryClient = useQueryClient();
  const tagsQuery = useUserTags();
  const [choice, setChoice] = useState('');
  const [error, setError] = useState<string | null>(null);
  const busy = confirmation.busy;
  const applied = new Set(memberships.filter((m) => m.source !== 'dismissed').map((m) => m.tag));
  const options = (tagsQuery.data?.tags ?? [])
    .filter((tag) => !applied.has(tag.name))
    .map((tag) => ({ value: tag.name, label: tag.name, description: tag.description }));
  const summary = tagLimitsSummary(limits, (value) =>
    money.format(value, { maximumFractionDigits: 2 }),
  );

  const afterWrite = async () => {
    await Promise.all([
      onChanged(),
      queryClient.invalidateQueries({ queryKey: USERS_LIST_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: USER_TAGS_QUERY_KEY }),
    ]);
  };

  const add = async () => {
    if (!choice) return;
    const tag = choice;
    const failure = await confirmation.perform({
      key: 'user tags',
      run: async () => {
        await addUserTagMembers(tag, [userId]);
        setChoice('');
        await afterWrite();
      },
    });
    setError(failure);
  };

  const remove = async (membership: UserTagMembership) => {
    const failure = await confirmation.perform({
      key: 'user tags',
      run: async () => {
        await removeUserTagMember(membership.tag, userId);
        await afterWrite();
      },
    });
    setError(failure);
  };

  return (
    <section aria-label="User tags" className={`${SECTION} space-y-2`}>
      <div className={FIELD_LABEL}>Tags</div>
      <div className="flex flex-wrap items-center gap-1.5">
        {memberships.length === 0 && <span className="text-[12px] text-gray-400">No tags.</span>}
        {memberships.map((membership) => (
          <TagChip key={membership.tag} name={membership.tag} source={membership.source}>
            <button
              type="button"
              onClick={() => void remove(membership)}
              disabled={busy}
              className="ml-0.5 rounded-full px-0.5 leading-none opacity-60 hover:opacity-100 disabled:cursor-not-allowed"
              aria-label={
                membership.source === 'dismissed'
                  ? `Forget that ${membership.tag} was dismissed`
                  : `Remove ${membership.tag}`
              }
              title={
                membership.source === 'dismissed'
                  ? 'Forget the dismissal: the automatic rule reads all recent requests again'
                  : `Remove ${membership.tag}`
              }
            >
              ×
            </button>
          </TagChip>
        ))}
        {options.length > 0 && (
          <span className="ml-1 inline-flex items-center gap-1.5">
            <Select
              aria-label="Tag to add"
              value={choice}
              onValueChange={setChoice}
              options={[{ value: '', label: 'Add a tag…' }, ...options]}
              disabled={busy}
              className="w-40"
            />
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => void add()}
              disabled={busy || !choice}
            >
              Add
            </button>
          </span>
        )}
      </div>
      {summary && (
        <p className="text-[12px] text-gray-700" data-testid="user-tag-limits">
          Tag limits: {summary}
        </p>
      )}
      {memberships.some((m) => m.source === 'auto') && (
        <p className="text-[11px] text-gray-500">
          Removing an automatic tag keeps it dismissed: it comes back only if requests sent after
          match its rule again.
        </p>
      )}
      {error && (
        <p role="alert" className="text-[12px] text-red-600">
          {error}
        </p>
      )}
    </section>
  );
}
