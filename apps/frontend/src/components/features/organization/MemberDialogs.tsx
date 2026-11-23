'use client';

import { useRef, useState } from 'react';
import { AdminLoading, AdminNotice } from '@/components/features/admin/AdminPrimitives';
import { CHECKBOX, NOTICE_ACTION } from '@/components/features/admin/adminStyles';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { useT } from '@/components/providers/useT';
import { InputField } from '@/components/ui/InputField';
import type { MemberKey, Organization, OrganizationMember } from '@/lib/api/organization';
import {
  useOrganizationMemberModels,
  useRemoveOrganizationMember,
  useRestoreOrganizationMember,
  useRevokeMemberKey,
  useSetMemberModels,
  useSetMemberMonthlyLimit,
} from '@/lib/hooks';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useMoney } from '@/lib/hooks/useMoney';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount, moneyCurrency } from '@/lib/utils/money';
import { readLimit } from './limits';
import { memberName } from './text';

/**
 * Which member dialog is open: its monthly limit, its models, the revoking of
 * one of its API keys, its removal, or its return.
 */
export type MemberDialog =
  | { mode: 'limit'; member: OrganizationMember }
  | { mode: 'models'; member: OrganizationMember }
  | { mode: 'revoke-key'; member: OrganizationMember; apiKey: MemberKey }
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
  onRetryOrganization,
  onClose,
}: {
  member: OrganizationMember;
  organization: Organization | undefined;
  onRetryOrganization?: () => void;
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
      {/* Save is off without the organization. A read that failed gives the
          reason and the way to try again; a read in progress needs neither. */}
      {!organization && onRetryOrganization ? (
        <AdminNotice
          role="alert"
          action={
            <button type="button" className={NOTICE_ACTION} onClick={onRetryOrganization}>
              {t('organization.retry', 'Retry')}
            </button>
          }
        >
          {t(
            'organization.limit_dialog.no_organization',
            'The organization could not be read, so the limit cannot be saved.',
          )}
        </AdminNotice>
      ) : null}
    </ConfirmDialog>
  );
}

/**
 * The models of one member. A checked model is on. The organization turns a
 * model off here, and on again; a model that the platform administrator
 * turned off is shown and cannot be changed.
 */
function ModelsDialog({ member, onClose }: { member: OrganizationMember; onClose: () => void }) {
  const t = useT();
  const toast = useAccountToast();
  const models = useOrganizationMemberModels(member.user_id);
  const save = useSetMemberModels();
  // The models that are off, as the reader last set them; until then, as read.
  const [draft, setDraft] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  const stored = models.data?.disabled_models ?? [];
  const off = draft ?? stored;
  const platformOff = models.data?.platform_disabled_models ?? [];
  // A model the organization turned off stays in the list when the platform
  // no longer offers it, so that it can still be turned on again.
  const available = models.data
    ? [...new Set([...models.data.available, ...stored])].filter(
        (model) => !platformOff.includes(model),
      )
    : [];

  function toggle(model: string) {
    setDraft(off.includes(model) ? off.filter((one) => one !== model) : [...off, model]);
  }

  async function confirm(): Promise<void> {
    if (submitting.current || !models.data) return;
    const next = [...off].sort();
    // The list that is stored is not sent again.
    if (JSON.stringify(next) === JSON.stringify([...stored].sort())) {
      onClose();
      return;
    }
    submitting.current = true;
    setError(null);
    try {
      await save.mutateAsync({ userId: member.user_id, disabledModels: next });
      toast.success(t('organization.models_dialog.saved', 'Models saved.'));
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  const row = 'flex items-center gap-2 rounded-md border px-3 py-1.5 text-[12px]';
  return (
    <ConfirmDialog
      title={t('organization.models_dialog.title', 'Models')}
      confirmLabel={t('organization.save', 'Save')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={save.isPending}
      confirmDisabled={!models.data}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submitting.current) onClose();
      }}
    >
      <p>
        {fill(
          t(
            'organization.models_dialog.body',
            'The models that {member} can use. A model that is not checked is refused for this member.',
          ),
          { member: who(member) },
        )}
      </p>
      {models.isLoading ? (
        <AdminLoading size="sm" label={t('common.loading', 'Loading...')} />
      ) : !models.data ? (
        <AdminNotice
          role="alert"
          action={
            <button type="button" className={NOTICE_ACTION} onClick={() => void models.refetch()}>
              {t('organization.retry', 'Retry')}
            </button>
          }
        >
          {getErrorMessage(models.error)}
        </AdminNotice>
      ) : (
        <div className="max-h-72 space-y-1.5 overflow-y-auto">
          {available.length === 0 && platformOff.length === 0 ? (
            <p className="text-[12px] text-gray-400">
              {t('organization.models_dialog.none', 'The platform offers this member no model.')}
            </p>
          ) : null}
          {available.map((model) => {
            const isOff = off.includes(model);
            return (
              <label
                key={model}
                className={`${row} cursor-pointer transition ${
                  isOff
                    ? 'border-red-200 bg-red-50 text-gray-500'
                    : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300'
                }`}
              >
                <input
                  type="checkbox"
                  className={CHECKBOX}
                  checked={!isOff}
                  disabled={save.isPending}
                  onChange={() => toggle(model)}
                />
                <span className="truncate font-medium">{model}</span>
              </label>
            );
          })}
          {platformOff.map((model) => (
            <label
              key={model}
              className={`${row} cursor-not-allowed border-gray-200 bg-gray-50 text-gray-500`}
            >
              <input type="checkbox" className={CHECKBOX} checked={false} disabled readOnly />
              <span className="truncate font-medium">{model}</span>
              <span className="ml-auto shrink-0 text-[11px] text-gray-400">
                {t('organization.models_dialog.platform_off', 'off by the platform administrator')}
              </span>
            </label>
          ))}
        </div>
      )}
    </ConfirmDialog>
  );
}

function RevokeKeyDialog({
  member,
  apiKey,
  onClose,
}: {
  member: OrganizationMember;
  apiKey: MemberKey;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useAccountToast();
  const revoke = useRevokeMemberKey();
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const keyName = apiKey.name ?? t('console.projects.unnamed_key', 'Unnamed key');

  async function confirm(): Promise<void> {
    if (submitting.current) return;
    submitting.current = true;
    setError(null);
    try {
      await revoke.mutateAsync({ userId: member.user_id, keyId: apiKey.id });
      toast.success(t('organization.revoke_dialog.done', 'API key revoked.'));
      onClose();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <ConfirmDialog
      title={t('organization.revoke_dialog.title', 'Revoke API key')}
      confirmLabel={t('organization.revoke_dialog.confirm', 'Revoke key')}
      cancelLabel={t('common.cancel', 'Cancel')}
      busyLabel={t('common.loading', 'Loading...')}
      busy={revoke.isPending}
      tone="danger"
      error={error}
      onConfirm={() => void confirm()}
      onCancel={() => {
        if (!submitting.current) onClose();
      }}
    >
      <p>
        {fill(
          t(
            'organization.revoke_dialog.body',
            'The API key "{key}" of {member} stops working at once. Requests with it are refused.',
          ),
          { key: keyName, member: who(member) },
        )}
      </p>
      <p>
        {t(
          'organization.revoke_dialog.after',
          'A revoked key stays revoked. The member can make a new one.',
        )}
      </p>
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
  onRetryOrganization,
  onClose,
}: {
  dialog: MemberDialog | null;
  /** For the limit dialog: the organization's own limit and its default. */
  organization: Organization | undefined;
  /**
   * Given when the read of the organization failed: reads it again. The limit
   * dialog that has no organization then says so, and offers this.
   */
  onRetryOrganization?: () => void;
  onClose: () => void;
}): JSX.Element | null {
  if (!dialog) return null;
  const key = `${dialog.mode}-${dialog.member.user_id}`;
  if (dialog.mode === 'limit') {
    return (
      <LimitDialog
        key={key}
        member={dialog.member}
        organization={organization}
        onRetryOrganization={onRetryOrganization}
        onClose={onClose}
      />
    );
  }
  if (dialog.mode === 'models') {
    return <ModelsDialog key={key} member={dialog.member} onClose={onClose} />;
  }
  if (dialog.mode === 'revoke-key') {
    return (
      <RevokeKeyDialog
        key={`${key}-${dialog.apiKey.id}`}
        member={dialog.member}
        apiKey={dialog.apiKey}
        onClose={onClose}
      />
    );
  }
  if (dialog.mode === 'remove') {
    return <RemoveDialog key={key} member={dialog.member} onClose={onClose} />;
  }
  return <RestoreDialog key={key} member={dialog.member} onClose={onClose} />;
}
