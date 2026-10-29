'use client';

import { useCallback, useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { useT } from '@/components/providers/useT';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  deleteSourceFile,
  listSourceFiles,
  type BatchSourceFile,
  type BatchSourceFileList,
} from '@/lib/api/batches';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';

import { formatBytes, formatTime } from './batchText';

/**
 * The user's uploaded source files, to free storage: list and delete, nothing else.
 *
 * Deleting a file never stops a batch already submitted from it; the file can
 * no longer start or be copied into a new one.
 */
export function SourceFilesPanel({
  onPick,
  onDeleted,
  refreshKey = 0,
}: {
  /** Use a listed file as the source of the batch being created. */
  onPick?: (file: BatchSourceFile) => void;
  onDeleted?: (fileId: string) => void;
  refreshKey?: number;
}) {
  const t = useT();
  const toast = useAccountToast();
  const [list, setList] = useState<BatchSourceFileList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setList(await listSourceFiles());
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  async function remove(file: BatchSourceFile) {
    if (
      !window.confirm(
        t(
          'batches.files.confirm_delete',
          'Delete this file? Batches already submitted from it keep running, but it can no longer be used or copied from.',
        ),
      )
    )
      return;
    setBusy(file.id);
    try {
      await deleteSourceFile(file.id);
      onDeleted?.(file.id);
      await load();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      aria-labelledby="batch-files-heading"
      className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200"
    >
      <h2 id="batch-files-heading" className="text-base font-semibold text-gray-900">
        {t('batches.files.title', 'Uploaded files')}
      </h2>
      {list && (
        <p className="mt-1 text-sm text-gray-500">
          {fill(
            t(
              'batches.files.usage',
              '{used} of {limit} used (stored copies of submitted batches included). Files are deleted automatically when they expire.',
            ),
            {
              used: formatBytes(list.usage.stored_bytes),
              limit: formatBytes(list.files_max_total_bytes),
            },
          )}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {list && list.data.length === 0 && (
        <p className="mt-3 text-sm text-gray-500">
          {t('batches.files.empty', 'You have no uploaded files.')}
        </p>
      )}
      {list && list.data.length > 0 && (
        <ul className="mt-3 divide-y divide-gray-100">
          {list.data.map((file) => (
            <li
              key={file.id}
              className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0 text-sm">
                <p className="truncate font-medium text-gray-900">{file.filename ?? file.id}</p>
                <p className="text-xs text-gray-500">
                  {fill(
                    t(
                      'batches.files.meta',
                      '{size} · {lines} lines · uploaded {created} · kept until {expires}',
                    ),
                    {
                      size: formatBytes(file.bytes),
                      lines: String(file.line_count ?? '—'),
                      created: formatTime(file.created_at),
                      expires: formatTime(file.expires_at),
                    },
                  )}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                {onPick && (
                  <Button size="sm" variant="secondary" onClick={() => onPick(file)}>
                    {t('batches.files.use', 'Use this file')}
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy === file.id}
                  onClick={() => void remove(file)}
                >
                  {t('batches.files.delete', 'Delete')}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
