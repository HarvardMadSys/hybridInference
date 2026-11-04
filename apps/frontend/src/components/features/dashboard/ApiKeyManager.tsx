'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useRef, useState } from 'react';
import type { ApiKeyListItem } from '@/lib/api/user';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useRegenerateApiKey,
  useRenameApiKey,
} from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { KeyActionsMenu } from './KeyActionsMenu';
import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';
import { formatUsd } from '@/lib/utils/currency';
import { keyDetailHref, limitsSummary } from './keyLimits';

function EyeIcon(): JSX.Element {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon(): JSX.Element {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10.6 5.1A9.9 9.9 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-3.3 4.2M6.6 6.6A17.6 17.6 0 0 0 2 12s3.5 7 10 7a9.9 9.9 0 0 0 4.2-.9" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="m2 2 20 20" />
    </svg>
  );
}

function CopyIcon(): JSX.Element {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function statusClassName(status: string): string {
  if (status === 'active') {
    return 'bg-green-50 text-green-700 ring-green-200';
  }
  if (status === 'revoked') {
    return 'bg-gray-50 text-gray-600 ring-gray-200';
  }
  return 'bg-yellow-50 text-yellow-700 ring-yellow-200';
}

/** A typed key label as the server stores it: trimmed, blank meaning none. */
function cleanName(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

const GRID_COLUMNS =
  'md:grid md:grid-cols-[minmax(0,2.6fr)_auto_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.4fr)_auto]';

type KeyAction =
  | { type: 'create' }
  | { type: 'rename' | 'regenerate' | 'delete'; key: ApiKeyListItem };

export function ApiKeyManager(): JSX.Element {
  const toast = useAccountToast();
  const router = useRouter();
  const branding = useBranding();
  const t = useT();
  const fmt = useFormatter();
  const formatDate = (value?: string | null): string =>
    value
      ? fmt.date(value, { year: 'numeric', month: 'short', day: 'numeric' })
      : t('dashboard.keys.never', 'Never');
  const statusLabel = (status: string): string =>
    status === 'active'
      ? t('dashboard.keys.status_active', 'active')
      : status === 'revoked'
        ? t('dashboard.keys.status_revoked', 'revoked')
        : status;
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [revealedKeys, setRevealedKeys] = useState<Set<string>>(new Set());
  const [action, setAction] = useState<KeyAction | null>(null);
  const [keyName, setKeyName] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const submittingRef = useRef(false);

  const { data: apiKeysResponse, isLoading: isLoadingKeys, error: apiKeysError } = useApiKeys();
  const createMutation = useCreateApiKey();
  const deleteMutation = useDeleteApiKey();
  const regenerateMutation = useRegenerateApiKey();
  const renameMutation = useRenameApiKey();

  const isLoading =
    createMutation.isPending ||
    deleteMutation.isPending ||
    regenerateMutation.isPending ||
    renameMutation.isPending;

  const apiKeys = apiKeysResponse?.keys ?? [];
  const liveKeys = apiKeys.filter((key) => key.status !== 'revoked');
  const revokedKeys = apiKeys.filter((key) => key.status === 'revoked');

  function showNewKey(value: string): void {
    setNewApiKey(value);
    setShowKey(true);
  }

  function openAction(next: KeyAction): void {
    setKeyName(next.type === 'rename' ? (next.key.name ?? '') : '');
    setActionError(null);
    setAction(next);
  }

  function closeAction(): void {
    if (!submittingRef.current) setAction(null);
  }

  async function confirmAction(): Promise<void> {
    if (!action || submittingRef.current) return;
    const name = cleanName(keyName);
    if (action.type === 'rename' && name === (action.key.name ?? null)) {
      setAction(null);
      return;
    }
    submittingRef.current = true;
    setActionError(null);
    try {
      if (action.type === 'create') {
        const response = await createMutation.mutateAsync(name);
        showNewKey(response.api_key);
        toast.success(
          t('dashboard.keys.created_toast', 'API Key created successfully! Please save it now.'),
        );
      } else if (action.type === 'rename') {
        await renameMutation.mutateAsync({ keyPrefix: action.key.key_prefix, name });
        toast.success(t('dashboard.keys.renamed_toast', 'API key renamed.'));
      } else if (action.type === 'regenerate') {
        const response = await regenerateMutation.mutateAsync(action.key.key_prefix);
        showNewKey(response.api_key);
        toast.success(
          t(
            'dashboard.keys.regenerated_toast',
            'API Key regenerated successfully! Please save it now.',
          ),
        );
      } else {
        await deleteMutation.mutateAsync(action.key.key_prefix);
        setNewApiKey(null);
        setShowKey(false);
        toast.success(
          action.key.status === 'active'
            ? t('dashboard.keys.revoked_toast', 'API key revoked. It can no longer be used.')
            : t('dashboard.keys.removed_toast', 'API Key removed.'),
        );
      }
      setAction(null);
    } catch (err) {
      setActionError(getErrorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  }

  const editingName = action?.type === 'create' || action?.type === 'rename';
  const actionLabel =
    action?.type === 'create'
      ? t('dashboard.keys.create', 'Create API Key')
      : action?.type === 'rename'
        ? t('dashboard.keys.rename', 'Rename')
        : action?.type === 'regenerate'
          ? t('dashboard.keys.regenerate', 'Regenerate')
          : action?.type === 'delete' && action.key.status === 'active'
            ? t('dashboard.keys.revoke', 'Revoke')
            : t('dashboard.keys.remove', 'Remove');
  const actionDescription =
    action?.type === 'create'
      ? t(
          'dashboard.keys.name_prompt',
          'Name this key (optional), for example "laptop" or "CI". All of your keys share your account\'s quota; each can also have limits of its own.',
        )
      : action?.type === 'rename'
        ? t('dashboard.keys.rename_prompt', 'Rename this key. Leave it blank to remove the name.')
        : action?.type === 'regenerate'
          ? t(
              'dashboard.keys.confirm_regenerate',
              'Regenerate API Key? The old key will be immediately invalidated. The key keeps its name, limits and spend.',
            )
          : action?.type === 'delete' && action.key.status === 'active'
            ? t(
                'dashboard.keys.confirm_revoke',
                'This key will stop working immediately. Its usage history will remain available.',
              )
            : t('dashboard.keys.confirm_remove', 'Remove this revoked API Key from the dashboard?');

  function toggleReveal(prefix: string): void {
    setRevealedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(prefix)) next.delete(prefix);
      else next.add(prefix);
      return next;
    });
  }

  function handleCopyKey(value: string): void {
    void navigator.clipboard.writeText(value);
    toast.success(t('dashboard.keys.copied_toast', 'API key copied to clipboard'));
  }

  function renderRow(apiKey: ApiKeyListItem): JSX.Element {
    const canReveal = Boolean(apiKey.api_key);
    const revealed = revealedKeys.has(apiKey.key_prefix);
    const shownValue = canReveal && revealed ? apiKey.api_key! : apiKey.key_masked;
    return (
      <div
        key={apiKey.key_prefix}
        className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-3 px-4 py-4 md:col-span-full md:grid-cols-subgrid md:items-center"
      >
        <div className="col-span-full min-w-0 md:col-span-1">
          <p className="mb-1.5 flex min-w-0 flex-wrap items-baseline gap-x-2 text-sm">
            <Link
              href={keyDetailHref(apiKey.key_prefix)}
              title={apiKey.name ?? undefined}
              className={`truncate hover:underline ${
                apiKey.name ? 'font-medium text-gray-900' : 'italic text-gray-400'
              }`}
            >
              {apiKey.name ?? t('dashboard.keys.unnamed', 'Unnamed key')}
            </Link>
            {apiKey.status === 'active' && (
              <span className="text-xs text-gray-500">{limitsSummary(apiKey, t)}</span>
            )}
          </p>
          <div className="grid grid-cols-[minmax(0,1fr)_1.75rem_1.75rem] items-center gap-2">
            <code
              className={`min-w-0 flex-1 rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200 ${revealed ? 'break-all' : 'truncate'}`}
            >
              {shownValue}
            </code>
            {canReveal && (
              <>
                <button
                  type="button"
                  onClick={() => toggleReveal(apiKey.key_prefix)}
                  title={
                    revealed
                      ? t('dashboard.keys.hide', 'Hide key')
                      : t('dashboard.keys.show', 'Show key')
                  }
                  aria-label={
                    revealed
                      ? t('dashboard.keys.hide', 'Hide key')
                      : t('dashboard.keys.show', 'Show key')
                  }
                  className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                >
                  {revealed ? <EyeOffIcon /> : <EyeIcon />}
                </button>
                <button
                  type="button"
                  onClick={() => handleCopyKey(apiKey.api_key!)}
                  title={t('dashboard.keys.copy', 'Copy key')}
                  aria-label={t('dashboard.keys.copy', 'Copy key')}
                  className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                >
                  <CopyIcon />
                </button>
              </>
            )}
          </div>
          {!apiKey.api_key && apiKey.status === 'active' && (
            <p className="mt-1 text-xs text-gray-500">
              {t(
                'dashboard.keys.full_key_once',
                'Full key is only shown immediately after creation or regeneration.',
              )}
            </p>
          )}
        </div>
        <span
          className={`col-start-1 row-start-2 inline-flex h-6 w-fit items-center self-center whitespace-nowrap rounded-full md:col-start-auto md:row-start-auto px-2.5 text-xs font-medium capitalize ring-1 ring-inset ${statusClassName(
            apiKey.status,
          )}`}
        >
          {statusLabel(apiKey.status)}
        </span>
        <div
          className="col-span-full text-sm text-gray-600 md:col-span-1"
          title={apiKey.created_at ? fmt.dateTime(apiKey.created_at) : undefined}
        >
          <span className="md:hidden font-medium text-gray-500">
            {t('dashboard.keys.created_inline', 'Created: ')}
          </span>
          {formatDate(apiKey.created_at)}
        </div>
        <div
          className="col-span-full text-sm text-gray-600 md:col-span-1"
          title={apiKey.last_used_at ? fmt.dateTime(apiKey.last_used_at) : undefined}
        >
          <span className="md:hidden font-medium text-gray-500">
            {t('dashboard.keys.last_used_inline', 'Last used: ')}
          </span>
          {formatDate(apiKey.last_used_at)}
        </div>
        <div className="col-span-full text-sm text-gray-600 md:col-span-1">
          <span className="md:hidden font-medium text-gray-500">
            {t('dashboard.keys.spend_inline', 'Spend: ')}
          </span>
          <span className="whitespace-nowrap">
            {fill(t('dashboard.keys.spent_today', '{amount} today'), {
              amount: formatUsd(apiKey.spent_today_usd ?? 0),
            })}
          </span>{' '}
          <span className="whitespace-nowrap text-gray-500 md:block">
            {fill(t('dashboard.keys.spent_month', '{amount} this month'), {
              amount: formatUsd(apiKey.spent_month_usd ?? 0),
            })}
          </span>
        </div>
        <div className="col-start-2 row-start-2 flex justify-end md:col-start-auto md:row-start-auto">
          {apiKey.status === 'active' || apiKey.status === 'revoked' ? (
            <KeyActionsMenu
              label={fill(t('dashboard.keys.more_actions', 'Actions for {name}'), {
                name: apiKey.name ?? apiKey.key_prefix,
              })}
              disabled={isLoading}
              items={
                apiKey.status === 'active'
                  ? [
                      {
                        label: t('dashboard.keys.limits', 'Limits'),
                        onSelect: () => router.push(keyDetailHref(apiKey.key_prefix)),
                      },
                      {
                        label: t('dashboard.keys.rename', 'Rename'),
                        onSelect: () => openAction({ type: 'rename', key: apiKey }),
                      },
                      {
                        label: t('dashboard.keys.regenerate', 'Regenerate'),
                        onSelect: () => openAction({ type: 'regenerate', key: apiKey }),
                      },
                      {
                        label: t('dashboard.keys.revoke', 'Revoke'),
                        danger: true,
                        onSelect: () => openAction({ type: 'delete', key: apiKey }),
                      },
                    ]
                  : [
                      {
                        label: t('dashboard.keys.remove', 'Remove'),
                        danger: true,
                        onSelect: () => openAction({ type: 'delete', key: apiKey }),
                      },
                    ]
              }
            />
          ) : (
            <span className="text-sm text-gray-400">
              {t('dashboard.keys.no_actions', 'No actions')}
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900">
            {t('dashboard.keys.title', 'API Keys')}
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            {fill(
              t(
                'dashboard.keys.subtitle',
                'Manage keys used to authenticate requests to the {app_name} API.',
              ),
              { app_name: branding.appName },
            )}
          </p>
        </div>
        {!isLoadingKeys && (
          <Button
            onClick={() => openAction({ type: 'create' })}
            disabled={isLoading}
            isLoading={createMutation.isPending}
          >
            {t('dashboard.keys.create', 'Create API Key')}
          </Button>
        )}
      </div>

      {showKey && newApiKey && (
        <div className="mb-6 rounded-lg bg-yellow-50 p-4 ring-1 ring-inset ring-yellow-200">
          <p className="text-sm font-medium text-yellow-800 mb-2">
            {t('dashboard.keys.save_now', 'Save this key securely.')}
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
              {newApiKey}
            </code>
            <button
              type="button"
              onClick={() => handleCopyKey(newApiKey)}
              title={t('dashboard.keys.copy', 'Copy key')}
              aria-label={t('dashboard.keys.copy', 'Copy key')}
              className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
            >
              <CopyIcon />
            </button>
          </div>
        </div>
      )}

      {apiKeysError && (
        <div className="mb-4 rounded-md bg-red-50 px-4 py-3 text-sm text-red-700 ring-1 ring-inset ring-red-200">
          {t('dashboard.keys.load_failed', 'Failed to load API keys. Please try again later.')}
        </div>
      )}

      {isLoadingKeys && (
        <div className="mb-4 text-sm text-gray-500">{t('common.loading', 'Loading...')}</div>
      )}

      {!isLoadingKeys && !apiKeysError && apiKeys.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-4 py-5 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
          {t('dashboard.keys.empty', 'No API keys yet. Create one to start using the API.')}
        </div>
      )}

      {apiKeys.length > 0 && (
        <div className={`rounded-lg ring-1 ring-gray-200 ${GRID_COLUMNS}`}>
          <div className="hidden gap-4 rounded-t-lg bg-gray-50 px-4 py-3 text-xs font-medium uppercase tracking-wide text-gray-500 md:col-span-full md:grid md:grid-cols-subgrid md:items-center">
            <span>{t('dashboard.keys.col_key', 'API Key')}</span>
            <span>{t('dashboard.keys.col_status', 'Status')}</span>
            <span>{t('dashboard.keys.col_created', 'Created')}</span>
            <span>{t('dashboard.keys.col_last_used', 'Last Used')}</span>
            <span>{t('dashboard.keys.col_spend', 'Spend (UTC)')}</span>
            <span className="text-right">{t('dashboard.keys.col_actions', 'Actions')}</span>
          </div>
          <div className="divide-y divide-gray-200 md:contents">
            {[...liveKeys, ...revokedKeys].map(renderRow)}
          </div>
        </div>
      )}

      {revokedKeys.length > 0 && (
        <p className="mt-3 text-sm text-gray-500">
          {t(
            'dashboard.keys.revoked_hint',
            'Revoked keys can no longer call the API. They are kept here so you can review their usage history.',
          )}
        </p>
      )}
      {action && (
        <ConfirmDialog
          title={actionLabel}
          confirmLabel={actionLabel}
          cancelLabel={t('common.cancel', 'Cancel')}
          busyLabel={t('common.loading', 'Loading...')}
          busy={isLoading}
          tone={editingName ? 'default' : 'danger'}
          error={actionError}
          initialFocusRef={editingName ? nameInputRef : undefined}
          onConfirm={() => void confirmAction()}
          onCancel={closeAction}
        >
          <p>{actionDescription}</p>
          {action.type !== 'create' && (
            <p className="rounded-md bg-gray-50 p-2 font-medium text-gray-700">
              {action.key.name ? `${action.key.name} · ` : ''}
              {action.key.key_masked}
            </p>
          )}
          {editingName && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void confirmAction();
              }}
            >
              <InputField
                ref={nameInputRef}
                label={t('dashboard.keys.name_label', 'Name (optional)')}
                value={keyName}
                onChange={(event) => setKeyName(event.target.value)}
                maxLength={64}
                disabled={isLoading}
                autoComplete="off"
              />
              <button type="submit" className="hidden" tabIndex={-1} disabled={isLoading} />
            </form>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
