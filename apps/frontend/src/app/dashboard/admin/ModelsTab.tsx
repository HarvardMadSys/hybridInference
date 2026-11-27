'use client';

import Link from 'next/link';

import {
  ConfigCategorySection,
  ConfigurationNotices,
  useConfigurationEditor,
} from '@/components/features/admin/ConfigurationEditor';
import { useAccountToast } from '@/lib/hooks/useAccountToast';

import { ModelConcurrencySection } from './ModelConcurrencySection';
import { ModelVisibilitySection } from './ModelVisibilitySection';
import { MODEL_CATALOG_CATEGORY } from './settingsLayout';

/**
 * Admin → Models: the model catalog, and the runtime overrides that apply on
 * top of it without changing it -- the role each model requires, and which
 * models the per-user concurrency limit leaves out. A model's routes and
 * weights are on the Routing tab; the other application documents on
 * Settings → Application.
 *
 * Each section saves on its own. The catalog is edited as on Settings: a save
 * asks for the administrator's password, and the notice above it offers the
 * restart the catalog waits for. The missing required settings are Settings'
 * to list; the console's banner points there.
 */
export function ModelsTab() {
  const toast = useAccountToast();
  const editor = useConfigurationEditor();
  const onToast = (message: string) => toast.success(message);

  return (
    <div className="mt-6 space-y-4">
      <p className="max-w-3xl text-[12px] leading-relaxed text-gray-500">
        The model catalog, and the runtime overrides that apply on top of it without changing it.
        Create a model, or change its routes and weights, on the{' '}
        <Link
          href="/dashboard/admin/routing"
          prefetch={false}
          className="font-medium text-gray-700 underline hover:text-gray-900"
        >
          Routing
        </Link>{' '}
        tab.
      </p>
      <ConfigurationNotices editor={editor} showMissing={false} />
      <div className="flex flex-col gap-8 pt-2">
        <ConfigCategorySection editor={editor} categoryId={MODEL_CATALOG_CATEGORY} />
        <ModelVisibilitySection onToast={onToast} />
        <ModelConcurrencySection onToast={onToast} />
      </div>
      {/* The password confirmation a catalog save asks for. */}
      {editor.backupConfirmation.dialog}
    </div>
  );
}
