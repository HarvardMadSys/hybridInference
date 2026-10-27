'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  listRuntimeSettings,
  RuntimeSettingItem,
  applyRoleQuota,
  previewRoleQuotaApply,
  Role,
  RoleQuotaPreview,
} from '@/lib/api/admin';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSubTabs,
} from '@/components/features/admin/AdminPrimitives';
import { BUTTON_GHOST, BUTTON_SECONDARY } from '@/components/features/admin/adminStyles';
import {
  AddVariableControl,
  ConfigCategorySection,
  ConfigurationBackupSection,
  ConfigurationFilters,
  ConfigurationNotices,
  matchingSettingCount,
  useConfigurationEditor,
} from '@/components/features/admin/ConfigurationEditor';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { getErrorMessage } from '@/lib/utils/errors';

import { AlertMutesSection } from './AlertMutesSection';
import { AlertSnoozeSection } from './AlertSnoozeSection';
import { UsageInsightsSettingsSection } from './UsageInsightsSettingsSection';
import { ModelVisibilitySection } from './ModelVisibilitySection';
import { ModelConcurrencySection } from './ModelConcurrencySection';
import { BatchConcurrencySection } from './BatchConcurrencySection';
import { RuntimeSettingsSection } from './RuntimeSettingsSection';
import { SignupPolicySection } from './SignupPolicySection';
import {
  DEFAULT_SETTINGS_TAB,
  RUNTIME_GROUPS,
  SETTINGS_TABS,
  isNamedCategory,
  runtimeGroupOf,
  tabOfCategory,
  type RuntimeGroupKey,
  type SettingsBlock,
  type SettingsPanelKey,
  type SettingsTabDefinition,
  type SettingsTabKey,
} from './settingsLayout';

const QUOTA_KEY_PREFIX = 'user_daily_quota_';

const tabId = (key: SettingsTabKey) => `admin-settings-${key}-tab`;
const panelId = (key: SettingsTabKey) => `admin-settings-${key}-panel`;

/** The key of a section that is not a configuration category. */
function blockKey(block: Extract<SettingsBlock, { kind: 'runtime' | 'panel' }>): string {
  return block.kind === 'runtime' ? `runtime-${block.group}` : `panel-${block.panel}`;
}

/** Sections that edit the configuration, and so have nothing to show without it. */
function needsConfiguration(block: SettingsBlock): boolean {
  return (
    block.kind === 'config' ||
    block.kind === 'unnamed-categories' ||
    (block.kind === 'panel' && (block.panel === 'add-variable' || block.panel === 'backup'))
  );
}

/** What a sub-tab shows beside its name: unsaved changes, and missing settings. */
function TabMeta({ dirty, missing }: { dirty: number; missing: number }) {
  if (dirty === 0 && missing === 0) return null;
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      {dirty > 0 ? (
        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden="true" />
      ) : null}
      {missing > 0 ? (
        <span
          className="rounded bg-red-50 px-1 text-[11px] font-medium tabular-nums text-red-700"
          aria-hidden="true"
        >
          {missing}
        </span>
      ) : null}
      {/* One phrase, so the tab's name reads "Access, unsaved changes, 1 missing". */}
      <span className="sr-only">
        {`${dirty > 0 ? ', unsaved changes' : ''}${missing > 0 ? `, ${missing} missing` : ''}`}
      </span>
    </span>
  );
}

/**
 * Admin → Settings: every setting an administrator changes in the console, in
 * sub-tabs (see `settingsLayout`): Common, with the settings changed most
 * often, then one sub-tab per topic, then Setup, with what is usually set
 * once. A topic's sections sit together whatever stores them: the
 * database-backed configuration, the runtime settings, and the sections with
 * an API of their own.
 *
 * Every sub-tab stays mounted while another is shown, so a change waits
 * unsaved, wherever it was made, until it is saved or discarded. A search or
 * Missing only looks through the configuration on every sub-tab at once, and
 * hides the other sections until it is cleared.
 */
export function SettingsTab({
  initialTab = DEFAULT_SETTINGS_TAB,
  initialMissingOnly = false,
}: {
  initialTab?: SettingsTabKey;
  initialMissingOnly?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const editor = useConfigurationEditor({ initialMissingOnly });
  // One confirmation for the page's confirmed writes, so only one is ever asked for.
  const confirmation = useConfirmAction();
  const [activeTab, setActiveTab] = useState<SettingsTabKey>(initialTab);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSettingItem[]>([]);
  const [runtimeLoading, setRuntimeLoading] = useState(true);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [quotaLoadingRole, setQuotaLoadingRole] = useState<Role | null>(null);
  // Unsaved changes in the sections that are not configuration, by section,
  // so a sub-tab can say it has some while another is shown.
  const [unsavedBySection, setUnsavedBySection] = useState<Record<string, number>>({});
  const unsavedReporters = useRef(new Map<string, (count: number) => void>());

  useEffect(() => {
    setActiveTab(initialTab);
  }, [initialTab]);

  /** The section's reporter: the same function every render, so reporting runs only on a change. */
  const reportUnsaved = (key: string) => {
    let report = unsavedReporters.current.get(key);
    if (!report) {
      report = (count: number) =>
        setUnsavedBySection((prev) =>
          (prev[key] ?? 0) === count ? prev : { ...prev, [key]: count },
        );
      unsavedReporters.current.set(key, report);
    }
    return report;
  };

  const loadRuntimeSettings = useCallback(async () => {
    setRuntimeLoading(true);
    setRuntimeError(null);
    try {
      const resp = await listRuntimeSettings();
      setRuntimeSettings(resp.settings);
    } catch (e) {
      setRuntimeError(getErrorMessage(e));
    } finally {
      setRuntimeLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRuntimeSettings();
  }, [loadRuntimeSettings]);

  useEffect(
    () => () => {
      if (toastTimeoutRef.current !== null) {
        clearTimeout(toastTimeoutRef.current);
        toastTimeoutRef.current = null;
      }
    },
    [],
  );

  const flashToast = (msg: string) => {
    if (toastTimeoutRef.current !== null) {
      clearTimeout(toastTimeoutRef.current);
    }
    setToast(msg);
    toastTimeoutRef.current = setTimeout(() => {
      setToast(null);
      toastTimeoutRef.current = null;
    }, 3000);
  };

  /**
   * Re-render the server components after a runtime setting changed. The site
   * configuration they read follows some of these settings: sign-up and email
   * verification decide whether the email (SMTP) settings are required, and
   * user authentication whether the sign-in secrets are — so whether the
   * "required settings are missing" banner shows.
   */
  const refreshSiteConfig = () => router.refresh();

  const replaceSetting = (updated: RuntimeSettingItem) =>
    setRuntimeSettings((prev) => prev.map((s) => (s.key === updated.key ? updated : s)));

  // The preview is a read; applying is the write, confirmed with what the preview found.
  const onClickApply = async (role: Role) => {
    setQuotaLoadingRole(role);
    let preview: RoleQuotaPreview;
    try {
      preview = await previewRoleQuotaApply(role);
    } catch (e) {
      flashToast(`Preview failed: ${getErrorMessage(e)}`);
      return;
    } finally {
      setQuotaLoadingRole(null);
    }
    confirmation.request({
      title: `Apply the ${preview.role} quota to existing keys?`,
      confirmLabel: 'Apply quota',
      busyLabel: 'Applying…',
      tone: 'danger',
      body: (
        <p>
          This sets <code>quota_daily_cost_usd = ${preview.quota.toString()}</code> on{' '}
          <strong>{preview.keys_affected}</strong> active API keys belonging to{' '}
          <strong>{preview.users_affected}</strong> users with role <code>{preview.role}</code>.
          Custom per-key overrides will be lost.
        </p>
      ),
      run: async () => {
        const res = await applyRoleQuota(preview.role);
        flashToast(`Updated ${res.keys_updated} keys for role ${preview.role} to $${res.quota}`);
      },
    });
  };

  if (!editor.settled) {
    return (
      <div className="mt-6">
        <AdminLoading />
      </div>
    );
  }

  const { filtering } = editor;
  // Without the configuration (an older backend), a sub-tab that only edits it has nothing to show.
  const tabs = SETTINGS_TABS.filter(
    (tab) => editor.config !== null || tab.blocks.some((block) => !needsConfiguration(block)),
  );
  const currentTab = tabs.some((tab) => tab.key === activeTab) ? activeTab : tabs[0].key;

  const selectTab = (key: SettingsTabKey) => {
    setActiveTab(key);
    router.replace(key === DEFAULT_SETTINGS_TAB ? pathname : `${pathname}?tab=${key}`, {
      scroll: false,
    });
  };

  const clearFilters = () => {
    editor.setQuery('');
    editor.setMissingOnly(false);
    // Drops a `?missing=1` the page was opened with, so a reload does not filter again.
    selectTab(currentTab);
  };

  const tabMeta = (tab: SettingsTabDefinition) => {
    const groups = editor.groups.filter((group) => tabOfCategory(group.id) === tab.key);
    const unsavedElsewhere = tab.blocks.reduce(
      (total, block) =>
        block.kind === 'runtime' || block.kind === 'panel'
          ? total + (unsavedBySection[blockKey(block)] ?? 0)
          : total,
      0,
    );
    return (
      <TabMeta
        dirty={
          groups.reduce((total, group) => total + editor.dirtyCount(group), 0) + unsavedElsewhere
        }
        missing={groups.reduce(
          (total, group) => total + group.entries.filter((entry) => entry.missing).length,
          0,
        )}
      />
    );
  };

  const renderQuotaApply = (setting: RuntimeSettingItem, unsaved: boolean) =>
    setting.key.startsWith(QUOTA_KEY_PREFIX) ? (
      <button
        type="button"
        onClick={() => void onClickApply(setting.key.slice(QUOTA_KEY_PREFIX.length) as Role)}
        // It applies the saved quota, not the one being typed.
        disabled={unsaved || quotaLoadingRole !== null || confirmation.busy}
        title={unsaved ? 'Save or discard the new quota first' : undefined}
        className={BUTTON_SECONDARY}
      >
        {quotaLoadingRole === setting.key.slice(QUOTA_KEY_PREFIX.length)
          ? 'Loading...'
          : 'Apply to existing users'}
      </button>
    ) : null;

  const runtimeSection = (group: RuntimeGroupKey) => (
    <RuntimeSettingsSection
      id={`settings-${group}`}
      title={RUNTIME_GROUPS[group].title}
      description={RUNTIME_GROUPS[group].description}
      settings={runtimeSettings.filter((setting) => runtimeGroupOf(setting.key) === group)}
      // The catch-all is usually empty: it waits unseen rather than flash a spinner.
      loading={runtimeLoading && group !== 'other'}
      onSaved={replaceSetting}
      onAfterWrite={refreshSiteConfig}
      onToast={flashToast}
      renderExtra={group === 'quotas' ? renderQuotaApply : undefined}
      onUnsavedChange={reportUnsaved(`runtime-${group}`)}
    />
  );

  const panelSection = (panel: SettingsPanelKey) => {
    const onUnsavedChange = reportUnsaved(`panel-${panel}`);
    switch (panel) {
      case 'usage-insights':
        return (
          <UsageInsightsSettingsSection onToast={flashToast} onUnsavedChange={onUnsavedChange} />
        );
      case 'signup-policy':
        return (
          <SignupPolicySection
            id="settings-signup-policy"
            confirmation={confirmation}
            onToast={flashToast}
          />
        );
      case 'model-visibility':
        return <ModelVisibilitySection onToast={flashToast} onUnsavedChange={onUnsavedChange} />;
      case 'model-concurrency':
        return <ModelConcurrencySection onToast={flashToast} onUnsavedChange={onUnsavedChange} />;
      case 'batch-concurrency':
        return <BatchConcurrencySection onToast={flashToast} onUnsavedChange={onUnsavedChange} />;
      case 'alert-snooze':
        return <AlertSnoozeSection onToast={flashToast} />;
      case 'alert-mutes':
        return <AlertMutesSection onToast={flashToast} />;
      case 'add-variable':
        return <AddVariableControl editor={editor} />;
      case 'backup':
        return <ConfigurationBackupSection editor={editor} />;
    }
  };

  const renderBlock = (tab: SettingsTabDefinition, block: SettingsBlock): ReactNode => {
    // While a filter lists every sub-tab's categories, each says where it lives.
    const tag = filtering ? tab.label : undefined;
    if (block.kind === 'config') {
      return (
        <ConfigCategorySection
          key={`config-${block.category}`}
          editor={editor}
          categoryId={block.category}
          tag={tag}
        />
      );
    }
    if (block.kind === 'unnamed-categories') {
      return editor.groups
        .filter((group) => !isNamedCategory(group.id))
        .map((group) => (
          <ConfigCategorySection
            key={`config-${group.id}`}
            editor={editor}
            categoryId={group.id}
            tag={tag}
          />
        ));
    }
    // Not configuration: a filter hides it, but it stays mounted, and with
    // it any change still unsaved in it.
    return (
      <div key={blockKey(block)} hidden={filtering}>
        {block.kind === 'runtime' ? runtimeSection(block.group) : panelSection(block.panel)}
      </div>
    );
  };

  const matching = matchingSettingCount(editor);

  return (
    <div className="mt-6 space-y-4">
      <ConfigurationNotices editor={editor} />
      {runtimeError ? (
        <AdminNotice onDismiss={() => setRuntimeError(null)}>{runtimeError}</AdminNotice>
      ) : null}
      {editor.config ? <ConfigurationFilters editor={editor} /> : null}

      {filtering ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-600">
          <span aria-live="polite">
            {matching === 1 ? '1 setting matches' : `${matching} settings match`}, from every
            sub-tab. Other sections are hidden until the filter is cleared.
          </span>
          <button type="button" onClick={clearFilters} className={BUTTON_GHOST}>
            Clear filters
          </button>
        </div>
      ) : (
        <AdminSubTabs
          label="Settings sections"
          tabs={tabs.map((tab) => ({
            key: tab.key,
            label: tab.label,
            tabId: tabId(tab.key),
            panelId: panelId(tab.key),
            meta: tabMeta(tab),
          }))}
          active={currentTab}
          onSelect={selectTab}
        />
      )}

      {/* Feedback for the sections on every sub-tab. The live region is
          always there (so a screen reader announces each message) and takes
          no height: a message floats at the top right of the sections,
          pinned to the top of the window while they scroll, without pushing
          them down, and lets clicks through to whatever is under it. */}
      <div className="pointer-events-none sticky top-4 z-20 h-0">
        <div role="status" className="flex justify-end">
          {toast && (
            <div
              key={toast}
              className="animate-admin-reveal max-w-md rounded-lg bg-gray-900 px-3 py-2 text-[12px] text-white shadow-lg"
            >
              {toast}
            </div>
          )}
        </div>
      </div>

      {filtering && editor.visibleGroups.length === 0 ? (
        <AdminEmptyState
          title={
            editor.missingOnly && editor.config?.missing.length === 0 && !editor.query.trim()
              ? 'No required settings are missing.'
              : 'No settings match.'
          }
        />
      ) : null}

      {/* No display class on an element that takes `hidden`: one would override it. */}
      <div className="flex flex-col gap-8">
        {tabs.map((tab) => {
          const shown = filtering
            ? editor.visibleGroups.some((group) => tabOfCategory(group.id) === tab.key)
            : tab.key === currentTab;
          return (
            <div
              key={tab.key}
              id={panelId(tab.key)}
              role={filtering ? undefined : 'tabpanel'}
              aria-labelledby={filtering ? undefined : tabId(tab.key)}
              hidden={!shown}
            >
              {/* While a filter lists every sub-tab's categories, each is tagged instead. */}
              {tab.description && !filtering ? (
                <p className="mb-6 max-w-3xl text-[12px] leading-relaxed text-gray-500">
                  {tab.description}
                </p>
              ) : null}
              {/* A section with nothing to show leaves an empty wrapper: no gap for it. */}
              <div className="flex flex-col gap-8 [&>div:empty]:hidden">
                {tab.blocks.map((block) => renderBlock(tab, block))}
              </div>
            </div>
          );
        })}
      </div>

      {confirmation.dialog}
      {editor.backupConfirmation.dialog}
    </div>
  );
}
