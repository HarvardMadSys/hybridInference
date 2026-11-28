'use client';

import { useCallback, useState } from 'react';
import Link from 'next/link';

import { AdminNotice } from '@/components/features/admin/AdminPrimitives';
import {
  ConfigCategorySection,
  ConfigurationNotices,
  useConfigurationEditor,
} from '@/components/features/admin/ConfigurationEditor';
import { ADMIN_ROUTING_PATH, ADMIN_SETTINGS_PATH } from '@/components/features/configuration/paths';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import { ModelConcurrencySection } from './ModelConcurrencySection';
import { ModelVisibilitySection } from './ModelVisibilitySection';
import { MODEL_CATALOG_CATEGORY } from './settingsLayout';
import { useLeaveGuard } from './useLeaveGuard';

const LINK = 'font-medium text-gray-700 underline hover:text-gray-900';

/**
 * Admin → Routing → Models: the model catalog, and the runtime overrides
 * that apply on top of it without changing it -- the role each model requires,
 * and which models the per-user concurrency limit leaves out. A model's routes
 * and weights are on Routing → Routes; the other application documents on
 * Settings → Site & Setup.
 *
 * Each section saves on its own. The catalog is edited as on Settings: a save
 * asks for the administrator's password, and the notice above it offers the
 * restart the catalog waits for. The missing required settings are Settings'
 * to list; the console's banner points there. While any section holds an
 * unsaved change, leaving the page asks first: for Routes too, since each
 * Routing section is a page of its own.
 */
export function ModelsTab() {
  const toast = useAccountToast();
  const editor = useConfigurationEditor();
  const onToast = (message: string) => toast.success(message);

  // What each override section holds unsaved; the catalog's is the editor's.
  const [visibilityUnsaved, setVisibilityUnsaved] = useState(0);
  const [concurrencyUnsaved, setConcurrencyUnsaved] = useState(0);
  const reportVisibility = useCallback((count: number) => setVisibilityUnsaved(count), []);
  const reportConcurrency = useCallback((count: number) => setConcurrencyUnsaved(count), []);
  const catalog = editor.groups.find((group) => group.id === MODEL_CATALOG_CATEGORY);
  useLeaveGuard(
    (catalog ? editor.dirtyCount(catalog) : 0) + visibilityUnsaved + concurrencyUnsaved,
  );

  // A backend older than the catalog's category of its own lists it with the
  // other application documents, on Settings.
  const catalogOnSettings =
    editor.config !== null &&
    catalog === undefined &&
    editor.config.entries.some((entry) => entry.key === 'MODEL_CATALOG');

  return (
    <div className="space-y-4">
      <p className="max-w-3xl text-[12px] leading-relaxed text-gray-500">
        The model catalog, and the runtime overrides that apply on top of it without changing it.
        Create a model, or change its routes and weights, under{' '}
        <Link href={ADMIN_ROUTING_PATH} prefetch={false} className={LINK}>
          Routes
        </Link>
        .
      </p>
      <ConfigurationNotices editor={editor} showMissing={false} />
      {catalogOnSettings ? (
        <AdminNotice tone="warning">
          This backend lists the model catalog with the other application documents: edit it on{' '}
          <Link href={`${ADMIN_SETTINGS_PATH}?tab=setup`} prefetch={false} className={LINK}>
            Settings → Site & Setup
          </Link>
          .
        </AdminNotice>
      ) : null}
      <div className="flex flex-col gap-8 pt-2">
        <ConfigCategorySection editor={editor} categoryId={MODEL_CATALOG_CATEGORY} />
        <ModelVisibilitySection onToast={onToast} onUnsavedChange={reportVisibility} />
        <ModelConcurrencySection onToast={onToast} onUnsavedChange={reportConcurrency} />
      </div>
      {/* The password confirmation a catalog save asks for. */}
      {editor.backupConfirmation.dialog}
    </div>
  );
}
