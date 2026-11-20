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
import { readLimit } from './limits';
import { memberName } from './text';

/** Which member dialog is open: its monthly limit, its removal, or its return. */
export type MemberDialog =
  | { mode: 'limit'; member: OrganizationMember }
  | { mode: 'remove'; member: OrganizationMember }
  | { mode: 'restore'; member: OrganizationMember };

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
  // What the organization set for this member: the one limit this dialog
  // changes. The quota of the platform administrator is shown and stays.
  const own = moneyAmount(member, 'own_monthly_limit');
  const platform = moneyAmount(member, 'platform_monthly_limit');
  const [value, setValue] = useState(own === null ? '' : String(own));
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  // The dialog is for one amount: the field has the cursor when it opens.
  const fieldRef = useRef<HTMLInputElement>(null);

  // The limit of the organization is the ceiling, in the same currency as the
  // amount that is typed. An organization read in another one gives no
  // ceiling here: the gateway then decides.
  const ceiling =
    organization && moneyCurrency(organization) === currency
      ? moneyAmount(organization, 'monthly_limit')
      : null;
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
    // Not before the organization is read: its limit is the ceiling.
    if (submitting.current || !organization) return;
    const read = readLimit(value, own, currency, ceiling);
    if ('problem' in read) {
      setError(
        read.problem === 'invalid'
          ? fill(
              t(
                'organization.limit_dialog.invalid',
                'Enter an amount in {currency}, such as 5 or 12.50, or leave it empty.',
              ),
              { currency },
            )
          : fill(
              t(
                'organization.limit_dialog.above',
                'A member cannot have a limit above the limit of the organization, {amount}.',
              ),
              { amount: money.value(organization, 'monthly_limit') },
            ),
      );
      return;
    }
    // The limit that is stored is not sent again.
    if (!read.changed) {
      onClose();
      return;
    }
    const { amount } = read;
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
      confirmDisabled={!organization}
      error={error}
      initialFocusRef={fieldRef}
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
      {platform === null ? null : (
        <p>
          {fill(
            t(
              'organization.limit_dialog.platform',
              'The platform administrator set a limit of {amount} for this account. The lower of the two limits applies.',
            ),
            { amount: money.value(member, 'platform_monthly_limit') },
          )}
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void confirm();
        }}
      >
        <InputField
          ref={fieldRef}
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
