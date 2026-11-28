'use client';

import { useState } from 'react';

import { EXPORT_DAYS, exportOpenInfra, type ExportDays } from '@/lib/api/openinfra';
import { getErrorMessage } from '@/lib/utils/errors';
import { saveFile } from './download';
import type { InfraFormat } from './format';
import { InfraDialog, smallButton } from './InfraDialog';

/**
 * Export, for an administrator: up to 30 days of hourly rollups, the snapshot
 * and the served totals as one JSON file, through the admin API as this tab's
 * session.
 */
export function ExportControl({ f }: { f: InfraFormat }) {
  const { t } = f;
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState<ExportDays>(30);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    if (busy) return;
    setOpen(false);
    setError(null);
  };

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const { filename, blob } = await exportOpenInfra(days);
      saveFile(filename, blob, 'application/json');
      setOpen(false);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button type="button" className={smallButton} onClick={() => setOpen(true)}>
        {t('openinfra.export.open', 'Export')}
      </button>
      {open ? (
        <InfraDialog
          title={t('openinfra.export.title', 'Export data')}
          onClose={close}
          actions={
            <button type="button" className={smallButton} onClick={close} disabled={busy}>
              {t('openinfra.export.cancel', 'Cancel')}
            </button>
          }
          description={
            <p>
              {t(
                'openinfra.export.desc',
                'Download up to 30 days of hourly rollups, the current snapshot and the served totals as JSON. Administrators only.',
              )}
            </p>
          }
        >
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void download();
            }}
          >
            <label className="block text-sm font-medium text-gray-700" htmlFor="openinfra-days">
              {t('openinfra.export.days', 'Days')}
            </label>
            <select
              id="openinfra-days"
              value={days}
              onChange={(event) => setDays(Number(event.target.value) as ExportDays)}
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900"
            >
              {EXPORT_DAYS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            {error ? (
              <p role="alert" className="text-sm text-red-600">
                {error}
              </p>
            ) : null}
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-md bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-700 disabled:opacity-60"
            >
              {busy
                ? t('openinfra.export.downloading', 'Exporting…')
                : t('openinfra.export.download', 'Download JSON')}
            </button>
          </form>
        </InfraDialog>
      ) : null}
    </>
  );
}
