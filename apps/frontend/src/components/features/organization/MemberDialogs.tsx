'use client';

import { useRef, useState } from 'react';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { useT } from '@/components/providers/useT';
import { InputField } from '@/components/ui/InputField';
import type { Organization, OrganizationMember } from '@/lib/api/organization';
import {
  useRemoveOrganizationMember,
  useRestoreOrganizationMember,
  useSetMemberMonthlyLimit,
} from '@/lib/hooks';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useMoney } from '@/lib/hooks/useMoney';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount, moneyCurrency } from '@/lib/utils/money';
import { memberName } from './text';

/** Which member dialog is open: its monthly limit, its removal, or its return. */
export type MemberDialog =
  | { mode: 'limit'; member: OrganizationMember }
  | { mode: 'remove'; member: OrganizationMember }
  | { mode: 'restore'; member: OrganizationMember };

/**
 * A typed limit: `null` for an empty field (no limit of its own), a number
 * that is zero or more, or `undefined` for anything else.
 */
export function parseLimit(value: string): number | null | undefined {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

/** "Lei Wang (lei.wang@example.edu)", or the one of the two the member has. */
function who(member: OrganizationMember): string {
  const name = memberName(member);
  return member.email && member.email !== name ? `${name} (${member.email})` : name;
}

function LimitDialog({
  member,
  organization,
  onClose,
}: {
  member: OrganizationMember;
  organization: Organization | undefined;
  onClose: () => void;
}) {
  const t = useT();
  const money = useMoney();
  const toast = useAccountToast();
  const save = useSetMemberMonthlyLimit();
  // The limit is typed in the currency the member's own row was read in, so a
  // USD row is never relabelled and saved as another currency's amount.
  const currency = moneyCurrency(member);
  const own =
    member.monthly_limit_source === 'member' ? moneyAmount(member, 'monthly_limit') : null;
  const initial = own === null ? '' : String(own);
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  const ceiling = moneyAmount(organization, 'monthly_limit');
  const fallback = moneyAmount(organization, 'default_member_monthly_limit');
  const hint =
    fallback === null
      ? t('organization.limit_dialog.hint_none', 'Leave empty for no limit of its own.')
      : fill(
          t(
            'organization.limit_dialog.hint_default',
            'Leave empty to use the default of the organization, {amount}.',
          ),
          { amount: money.value(organization, 'default_member_monthly_limit') },
        );

  async function confirm(): Promise<void> {
    if (submitting.current) return;
    const amount = parseLimit(value);
    if (amount === undefined) {
      setError(t('organization.limit_dialog.invalid', 'Enter an amount of zero or more.'));
      return;
    }
    if (amount !== null && ceiling !== null && amount > ceiling) {
      setError(
        fill(
          t(
            'organization.limit_dialog.above',
            'A member cannot have a limit above the limit of the organization, {amount}.',
          ),
          { amount: money.value(organization, 'monthly_limit') },
        ),
      );
      return;
    }
    // A limit left as it was read is not sent again: a migrated amount may
    // carry more decimal places than a new one may be typed with.
    if (value.trim() === initial) {
      onClose();
      return;
    }
    submitting.current = true;
    setError(null);
    try {
      await save.mutateAsync({ userId: member.user_id, limit: { amount, currency } });
      toast.success(
        amount === null
          ? t('organization.limit_dialog.cleared', 'Monthly limit cleared.')
          : t('organization.limit_dialog.saved', 'Monthly limit saved.'),
      );
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={t('organization.limit_dialog.title', 'Set monthly limit')}
      confirmLabel={t('organization.save', 'Save')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={save.isPending}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submitting.current) onClose();
      }}
    >
      <p>
        {fill(
          t(
            'organization.limit_dialog.body',
            'What {member} may spend in a calendar month (UTC). Requests over the limit are refused until the next month.',
          ),
          { member: who(member) },
        )}
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void confirm();
        }}
      >
        <InputField
          label={fill(t('organization.limit_dialog.label', 'Monthly limit ({currency})'), {
            currency,
          })}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          inputMode="decimal"
          placeholder={t('organization.limit.none', 'No limit')}
          hint={hint}
          disabled={save.isPending}
          autoComplete="off"
        />
        <button type="submit" className="hidden" tabIndex={-1} disabled={save.isPending} />
      </form>
    </ConfirmDialog>
  );
}

function RemoveDialog({ member, onClose }: { member: OrganizationMember; onClose: () => void }) {
  const t = useT();
  const toast = useAccountToast();
  const remove = useRemoveOrganizationMember();
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  async function confirm(): Promise<void> {
    if (submitting.current) return;
    submitting.current = true;
    setError(null);
    try {
      await remove.mutateAsync(member.user_id);
      toast.success(t('organization.remove_dialog.done', 'Member removed.'));
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={t('organization.remove_dialog.title', 'Remove from organization')}
      confirmLabel={t('organization.remove_dialog.confirm', 'Remove member')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={remove.isPending}
      tone="danger"
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submitting.current) onClose();
      }}
    >
      <p>
        {fill(t('organization.remove_dialog.intro', 'The account of {member} is suspended:'), {
          member: who(member),
        })}
      </p>
      <ul className="list-disc space-y-0.5 pl-5">
        <li>{t('organization.remove_dialog.sessions', 'its open sessions end;')}</li>
        <li>{t('organization.remove_dialog.keys', 'its API keys are revoked;')}</li>
        <li>
          {t(
            'organization.remove_dialog.sign_in',
            'it can no longer sign in, through single sign-on either.',
          )}
        </li>
      </ul>
      <p>
        {t(
          'organization.remove_dialog.after',
          'The usage history stays. You can restore the member later.',
        )}
      </p>
    </ConfirmDialog>
  );
}

function RestoreDialog({ member, onClose }: { member: OrganizationMember; onClose: () => void }) {
  const t = useT();
  const toast = useAccountToast();
  const restore = useRestoreOrganizationMember();
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  async function confirm(): Promise<void> {
    if (submitting.current) return;
    submitting.current = true;
    setError(null);
    try {
      await restore.mutateAsync(member.user_id);
      toast.success(t('organization.restore_dialog.done', 'Member restored.'));
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={t('organization.restore_dialog.title', 'Restore member')}
      confirmLabel={t('organization.restore_dialog.confirm', 'Restore member')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={restore.isPending}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submitting.current) onClose();
      }}
    >
      <p>
        {fill(
          t(
            'organization.restore_dialog.body',
            'The account of {member} becomes active again, and the member can sign in.',
          ),
          { member: who(member) },
        )}
      </p>
      <p>
        {t(
          'organization.restore_dialog.keys',
          'The revoked API keys stay revoked. The member makes new ones.',
        )}
      </p>
    </ConfirmDialog>
  );
}

/** The open member dialog, if any. Each opening starts from the member as it was read. */
export function MemberDialogs({
  dialog,
  organization,
  onClose,
}: {
  dialog: MemberDialog | null;
  /** For the limit dialog: the organization's own limit and its default. */
  organization: Organization | undefined;
  onClose: () => void;
}): JSX.Element | null {
  if (!dialog) return null;
  const key = `${dialog.mode}-${dialog.member.user_id}`;
  if (dialog.mode === 'limit') {
    return (
      <LimitDialog key={key} member={dialog.member} organization={organization} onClose={onClose} />
    );
  }
  if (dialog.mode === 'remove') {
    return <RemoveDialog key={key} member={dialog.member} onClose={onClose} />;
  }
  return <RestoreDialog key={key} member={dialog.member} onClose={onClose} />;
}
