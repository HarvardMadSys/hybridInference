'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { useT } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  deleteMyWebhook,
  getMyWebhook,
  listMyWebhookEvents,
  saveMyWebhook,
  type BatchWebhook,
  type BatchWebhookEvent,
} from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';

/**
 * The user's batch webhook: one HTTPS endpoint told when each of their
 * batches ends. The signing secret is shown once, when the webhook is created
 * or the secret rotated; deliveries are listed with how they went.
 *
 * Without `canEdit` (the user's role is below `BATCH_MIN_ROLE`) the webhook
 * can only be viewed and removed; the gateway refuses changes either way.
 */
export function WebhookSettings({ canEdit = true }: { canEdit?: boolean }) {
  const t = useT();
  const toast = useAccountToast();
  const [webhook, setWebhook] = useState<BatchWebhook | null>(null);
  const [events, setEvents] = useState<BatchWebhookEvent[]>([]);
  const [url, setUrl] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [secret, setSecret] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The form stays read-only until the stored settings have loaded (or failed
  // to): editing before then would be overwritten by the late answer.
  const [loaded, setLoaded] = useState(false);
  // Bumped by every write; a read that started before the latest write is
  // stale and dropped, so it can never put back settings a save replaced.
  const writes = useRef(0);

  const load = useCallback(async () => {
    const startedAt = writes.current;
    setLoadError(null);
    try {
      const current = await getMyWebhook();
      if (startedAt !== writes.current) return;
      setWebhook(current);
      setUrl(current.url ?? '');
      setEnabled(current.enabled ?? true);
      if (current.configured) {
        const recent = (await listMyWebhookEvents()).data;
        if (startedAt === writes.current) setEvents(recent);
      }
    } catch (err) {
      if (startedAt === writes.current) setLoadError(getErrorMessage(err));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(rotate: boolean) {
    if (
      rotate &&
      !window.confirm(
        t(
          'batches.webhook.confirm_rotate',
          'Rotate the signing secret? Deliveries are signed with the new secret from now on.',
        ),
      )
    ) {
      return;
    }
    writes.current += 1;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveMyWebhook({ url, enabled, rotate_secret: rotate });
      setWebhook(saved);
      setSecret(saved.secret ?? null);
      toast.success(t('batches.webhook.saved', 'Webhook saved.'));
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (
      !window.confirm(
        t('batches.webhook.confirm_delete', 'Remove the webhook? Undelivered events are dropped.'),
      )
    ) {
      return;
    }
    writes.current += 1;
    setSaving(true);
    try {
      await deleteMyWebhook();
      setWebhook({ configured: false });
      setUrl('');
      setSecret(null);
      setEvents([]);
      toast.success(t('batches.webhook.deleted', 'Webhook removed.'));
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <h2 className="text-base font-semibold text-gray-900">
        {t('batches.webhook.title', 'Webhook')}
      </h2>
      <p className="mt-1 text-sm text-gray-500">
        {t(
          'batches.webhook.subtitle',
          'Get a signed POST when one of your batches ends and its results can be fetched. Events name the batch only; fetch the results with your API key.',
        )}
      </p>
      {loadError && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {loadError}
        </p>
      )}
      {!canEdit && loaded && (
        <p role="note" className="mt-3 text-sm text-amber-800">
          {webhook?.configured
            ? t(
                'batches.webhook.read_only',
                'Your plan cannot change webhook settings. You can still view and remove your webhook.',
              )
            : t('batches.webhook.cannot_create', 'Your plan cannot set up a webhook.')}
        </p>
      )}
      {(canEdit || webhook?.configured) && (
        <form
          className="mt-4 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (loaded && canEdit) void save(false);
          }}
        >
          <InputField
            label={t('batches.webhook.url', 'Endpoint URL (https)')}
            type="url"
            value={url}
            placeholder="https://example.com/hooks/batches"
            onChange={(event) => setUrl(event.target.value)}
            disabled={!loaded || !canEdit}
            error={error ?? undefined}
            required
          />
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
              disabled={!loaded || !canEdit}
            />
            {t('batches.webhook.enabled', 'Send events')}
          </label>
          <div className="flex flex-wrap gap-2">
            {canEdit && (
              <Button type="submit" isLoading={saving} disabled={!loaded || !url}>
                {webhook?.configured
                  ? t('batches.webhook.update', 'Save')
                  : t('batches.webhook.create', 'Create webhook')}
              </Button>
            )}
            {webhook?.configured && (
              <>
                {canEdit && (
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={saving}
                    onClick={() => void save(true)}
                  >
                    {t('batches.webhook.rotate', 'Rotate secret')}
                  </Button>
                )}
                <Button
                  type="button"
                  variant="danger"
                  disabled={saving}
                  onClick={() => void remove()}
                >
                  {t('batches.webhook.remove', 'Remove')}
                </Button>
              </>
            )}
          </div>
        </form>
      )}
      {secret && (
        <div className="mt-4 rounded-lg bg-yellow-50 p-4 ring-1 ring-inset ring-yellow-200">
          <p className="text-sm font-medium text-yellow-800">
            {t(
              'batches.webhook.secret_once',
              'Signing secret — save it now, it is not shown again. Verify the webhook-signature header with it.',
            )}
          </p>
          <code className="mt-2 block break-all rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
            {secret}
          </code>
        </div>
      )}
      {events.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-medium text-gray-900">
            {t('batches.webhook.events', 'Recent deliveries')}
          </h3>
          <ul className="mt-2 divide-y divide-gray-100 text-xs text-gray-600">
            {events.map((event) => (
              <li key={event.id} className="flex flex-wrap justify-between gap-2 py-2">
                <span>
                  <code>{event.type}</code> · <code>{event.batch_id}</code>
                </span>
                <span>
                  {event.status === 'delivered'
                    ? t('batches.webhook.delivered', 'Delivered')
                    : event.status === 'failed'
                      ? t('batches.webhook.failed', 'Gave up')
                      : t('batches.webhook.pending', 'Retrying')}
                  {event.last_status_code ? ` · HTTP ${event.last_status_code}` : ''}
                  {event.attempts ? ` · ${event.attempts}×` : ''}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
