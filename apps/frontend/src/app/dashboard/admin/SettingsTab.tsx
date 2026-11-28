'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  listRuntimeSettings,
  RuntimeSettingItem,
  applyRoleQuota,
  previewRoleQuotaApply,
  Role,
  RoleQuotaApplyResult,
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
import { useMoney } from '@/lib/hooks/useMoney';
import { getErrorMessage } from '@/lib/utils/errors';
import { moneyCurrency } from '@/lib/utils/money';

import { AlertMutesSection } from './AlertMutesSection';
import { AlertSnoozeSection } from './AlertSnoozeSection';
import { UsageInsightsSettingsSection } from './UsageInsightsSettingsSection';
import { BatchConcurrencySection } from './BatchConcurrencySection';
import { RoutewiseSettingsSection } from './RoutewiseSettingsSection';
import { RuntimeSettingsSection } from './RuntimeSettingsSection';
import { SignupPolicySection } from './SignupPolicySection';
import { useLeaveGuard } from './useLeaveGuard';
import {
  CATEGORIES_ELSEWHERE,
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

/** A role quota's amount as the reader sees it, in the currency it was given in. */
function quotaAmount(source: RoleQuotaPreview | RoleQuotaApplyResult): number | null {
  return source.amount ?? (moneyCurrency(source) === 'USD' ? source.quota : null);
}

/** Sections that edit the configuration, and so have nothing to show without it. */
function needsConfiguration(block: SettingsBlock): boolean {
  return (
    block.kind === 'config' ||
    block.kind === 'unnamed-categories' ||
    (block.kind === 'panel' && (block.panel === 'add-variable' || block.panel === 'backup'))
  );
}

/** What a tab shows beside its name: unsaved changes, and missing settings. */
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

/** Whether a runtime setting matches a search, by key, label, description or group title. */
function runtimeMatchesQuery(setting: RuntimeSettingItem, query: string): boolean {
  const haystack =
    `${setting.key.replace(/_/g, ' ')} ${setting.description} ${RUNTIME_GROUPS[runtimeGroupOf(setting.key)].title}`.toLowerCase();
  return haystack.includes(query);
}

/**
 * Admin → Settings: every setting an administrator changes in the console but
 * the model configuration, which is on Admin → Routing → Models. Four task
 * tabs (see `settingsLayout`): Access & Limits, Providers & Routing,
 * Operations, and Site & Setup. Each tab holds its whole topic together —
 * the database-backed configuration, the runtime settings, and the sections
 * with an API of their own — so one change never needs two tabs.
 *
 * Every tab stays mounted while another is shown, so a change waits
 * unsaved, wherever it was made, until it is saved or discarded. Search
 * looks through the configuration and the runtime settings on every tab at
 * once; Missing only lists the configuration that still needs a value.
 * Sections with an API of their own stay mounted but hidden while a filter
 * is on, so a change unsaved in one is not lost.
 */
export function SettingsTab({
  initialTab = DEFAULT_SETTINGS_TAB,
  initialMissingOnly = false,
}: {
  initialTab?: SettingsTabKey;
  initialMissingOnly?: boolean;
}) {
  const money = useMoney();
  const router = useRouter();
  const pathname = usePathname();
  // The model catalog is on Routing → Models: not listed here, nor found by a search.
  const editor = useConfigurationEditor({
    initialMissingOnly,
    excludeCategories: CATEGORIES_ELSEWHERE,
  });
  // One confirmation for the page's confirmed writes, so only one is ever asked for.
  const confirmation = useConfirmAction();
  const [activeTab, setActiveTab] = useState<SettingsTabKey>(initialTab);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSettingItem[]>([]);
  const [runtimeLoading, setRuntimeLoading] = useState(true);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [quotaLoadingRole, setQuotaLoadingRole] = useState<Role | null>(null);
  const initialAnchorHandled = useRef(false);

  useEffect(() => {
    // Next's initial hash scroll can run before the sections exist. Wait for
    // both reads so the runtime sections above the target have their full height.
    if (!editor.settled || runtimeLoading || initialAnchorHandled.current) return;
    const target = document.getElementById(window.location.hash.slice(1));
    if (!target) {
      initialAnchorHandled.current = true;
      return;
    }
    if (target.closest('[hidden]')) {
      // A link to a section on another tab opens that tab first; this effect
      // runs again and scrolls once the section is shown. While a filter is
      // on, a hidden section stays hidden — there is nowhere to land.
      if (editor.filtering) {
        initialAnchorHandled.current = true;
        return;
      }
      const panel = target.closest('[id$="-panel"]');
      const key = panel?.id.replace('admin-settings-', '').replace('-panel', '');
      const owner = SETTINGS_TABS.find((tab) => tab.key === key);
      if (!owner || owner.key === activeTab) {
        initialAnchorHandled.current = true;
        return;
      }
      setActiveTab(owner.key);
      router.replace(
        owner.key === DEFAULT_SETTINGS_TAB
          ? `${pathname}${window.location.hash}`
          : `${pathname}?tab=${owner.key}${window.location.hash}`,
        { scroll: false },
      );
      return;
    }
    initialAnchorHandled.current = true;
    target.scrollIntoView({ block: 'start' });
  }, [editor.settled, editor.filtering, runtimeLoading, activeTab, pathname, router]);

  // Unsaved changes in the sections that are not configuration, by section,
  // so a tab can say it has some while another is shown.
  const [unsavedBySection, setUnsavedBySection] = useState<Record<string, number>>({});
  const unsavedReporters = useRef(new Map<string, (count: number) => void>());

  // A tab keeps its changes while another is shown; another admin tab,
  // being a page of its own, would not, so leaving with any unsaved asks first.
  useLeaveGuard(
    editor.groups.reduce((total, group) => total + editor.dirtyCount(group), 0) +
      Object.values(unsavedBySection).reduce((total, count) => total + count, 0),
  );

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
   * verification decide whether the email transport's settings are required, and
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
    // The amount is the preview's own, in the currency the preview was given in.
    const currency = moneyCurrency(preview);
    confirmation.request({
      title: `Apply the ${preview.role} quota to existing keys?`,
      confirmLabel: 'Apply quota',
      busyLabel: 'Applying…',
      tone: 'danger',
      body: (
        <p>
          This sets the daily quota to{' '}
          <strong>{money.format(quotaAmount(preview), undefined, currency)}</strong> on{' '}
          <strong>{preview.keys_affected}</strong> active API keys belonging to{' '}
          <strong>{preview.users_affected}</strong> users with role <code>{preview.role}</code>.
          Custom per-key overrides will be lost.
        </p>
      ),
      run: async () => {
        // The native amount, applied in the currency the preview was given in.
        const res = await applyRoleQuota(preview.role, currency);
        const applied = money.format(quotaAmount(res), undefined, moneyCurrency(res));
        flashToast(`Updated ${res.keys_updated} keys for role ${preview.role} to ${applied}`);
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
  // Without the configuration (an older backend), a tab that only edits it has nothing to show.
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

  // While searching, the runtime settings that match join the configuration
  // results; under Missing only they never match (nothing runtime is missing).
  const searchQuery = editor.query.trim().toLowerCase();
  const matchingRuntime =
    filtering && !editor.missingOnly && searchQuery
      ? runtimeSettings.filter((setting) => runtimeMatchesQuery(setting, searchQuery))
      : [];
  const runtimeForGroup = (group: RuntimeGroupKey) =>
    filtering
      ? matchingRuntime.filter((setting) => runtimeGroupOf(setting.key) === group)
      : runtimeSettings.filter((setting) => runtimeGroupOf(setting.key) === group);

  const runtimeSection = (group: RuntimeGroupKey) => (
    <RuntimeSettingsSection
      id={`settings-${group}`}
      title={RUNTIME_GROUPS[group].title}
      description={RUNTIME_GROUPS[group].description}
      settings={runtimeSettings.filter((setting) => runtimeGroupOf(setting.key) === group)}
      visibleKeys={
        filtering ? new Set(runtimeForGroup(group).map((setting) => setting.key)) : undefined
      }
      // The catch-all is usually empty: it waits unseen rather than flash a spinner.
      loading={runtimeLoading && group !== 'other'}
      onSaved={replaceSetting}
      onAfterWrite={refreshSiteConfig}
      onToast={flashToast}
      renderExtra={group === 'quotas' ? renderQuotaApply : undefined}
      onUnsavedChange={reportUnsaved(`runtime-${group}`)}
    />
  );

  const panelSection = (panel: SettingsPanelKey, tabActive = true) => {
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
      case 'batch-concurrency':
        return <BatchConcurrencySection onToast={flashToast} onUnsavedChange={onUnsavedChange} />;
      case 'routewise':
        return <RoutewiseSettingsSection onUnsavedChange={onUnsavedChange} active={tabActive} />;
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
    // While a filter lists every tab's categories, each says where it lives.
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
    // Not configuration: a filter hides a panel, and a runtime group with no
    // match, but both stay mounted, and with them any change still unsaved.
    const hidden =
      filtering &&
      (block.kind === 'panel' ||
        (block.kind === 'runtime' && runtimeForGroup(block.group).length === 0));
    return (
      <div key={blockKey(block)} hidden={hidden}>
        {block.kind === 'runtime'
          ? runtimeSection(block.group)
          : panelSection(block.panel, !filtering && tab.key === currentTab)}
      </div>
    );
  };

  const matching = matchingSettingCount(editor) + matchingRuntime.length;
  const totalUnsaved =
    editor.groups.reduce((total, group) => total + editor.dirtyCount(group), 0) +
    Object.values(unsavedBySection).reduce((total, count) => total + count, 0);
  const missingCount = editor.config?.missing.length ?? 0;

  return (
    <div className="mt-6 space-y-4">
      <ConfigurationNotices editor={editor} />
      {runtimeError ? (
        <AdminNotice onDismiss={() => setRuntimeError(null)}>{runtimeError}</AdminNotice>
      ) : null}
      {editor.config ? <ConfigurationFilters editor={editor} /> : null}

      {/* One line that answers "is anything wrong, and did I leave a change
          unsaved anywhere?" without opening each tab to check. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-gray-200 px-3 py-2 text-[12px] text-gray-600">
        {editor.config ? (
          missingCount > 0 ? (
            <span>
              <strong className="font-medium text-red-700">
                {missingCount} required {missingCount === 1 ? 'setting' : 'settings'} missing
              </strong>
              {!editor.missingOnly ? (
                <button
                  type="button"
                  onClick={() => editor.setMissingOnly(true)}
                  className="ml-2 font-medium text-gray-900 underline underline-offset-2"
                >
                  Show only these
                </button>
              ) : null}
            </span>
          ) : (
            <span>All required settings are set</span>
          )
        ) : null}
        <span aria-live="polite">
          {totalUnsaved > 0
            ? `${totalUnsaved} unsaved ${totalUnsaved === 1 ? 'change' : 'changes'} across all tabs`
            : 'No unsaved changes'}
        </span>
      </div>

      {filtering ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-gray-50 px-3 py-2 text-[12px] text-gray-600">
          <span aria-live="polite">
            {matching === 1 ? '1 setting matches' : `${matching} settings match`}, from every tab.
            Sections with their own API are hidden until the filter is cleared.
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

      {/* Feedback for the sections on every tab. The live region is
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

      {filtering && editor.visibleGroups.length === 0 && matchingRuntime.length === 0 ? (
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
            ? editor.visibleGroups.some((group) => tabOfCategory(group.id) === tab.key) ||
              tab.blocks.some(
                (block) => block.kind === 'runtime' && runtimeForGroup(block.group).length > 0,
              )
            : tab.key === currentTab;
          return (
            <div
              key={tab.key}
              id={panelId(tab.key)}
              role={filtering ? undefined : 'tabpanel'}
              aria-labelledby={filtering ? undefined : tabId(tab.key)}
              hidden={!shown}
            >
              {/* While a filter lists every tab's categories, each is tagged instead. */}
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
