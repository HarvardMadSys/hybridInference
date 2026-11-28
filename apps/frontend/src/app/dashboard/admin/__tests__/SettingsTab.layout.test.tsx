// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigEntry, ConfigResponse } from '@/lib/api/config';
import type { RuntimeSettingItem } from '@/lib/api/admin';
import { APIError } from '@/lib/utils/errors';

import { openSettingsTab, renderSettings } from './settingsTabHarness';

const replaceMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: replaceMock, refresh: vi.fn() }),
}));

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'general',
    description: '',
    type: 'str',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: '',
    default: null,
    source: 'database',
    restart_required: false,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: false,
    one_time: false,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

/** An alert webhook with no value: the channel picker edits the three together. */
function webhook(key: string): ConfigEntry {
  return entry({
    key,
    category: 'alerts',
    secret: true,
    one_time: true,
    is_set: false,
    value: null,
    source: 'default',
  });
}

/**
 * One setting or more in most categories a tab names, and one in a category
 * none names; nothing for Integrations, whose tab is then not listed. Where a
 * category is split, it has a setting at the top and one in a part of it
 * (`ADVANCED_SETTINGS`, `TOPIC_SECTIONS`), and some are usually set once. The
 * keys the layout does not name (`SESSION_HOURS`, `FUTURE_KNOB`) stay at the
 * top, under their variable's name. The model catalog's category is Routing →
 * Models', which this page leaves out.
 */
const CONFIG: ConfigResponse = {
  categories: [
    { id: 'models', label: 'Model catalog', description: 'Model metadata.' },
    { id: 'application', label: 'Application', description: 'Site documents.' },
    { id: 'general', label: 'General', description: 'Site identity.' },
    { id: 'security', label: 'Security', description: 'Secrets.' },
    { id: 'signup', label: 'Sign-up', description: 'Signup notices.' },
    { id: 'email', label: 'Email', description: 'Outgoing mail.' },
    { id: 'network', label: 'Network', description: 'Proxies.' },
    { id: 'providers', label: 'Providers', description: 'Model credentials.' },
    { id: 'routing', label: 'Routing', description: 'Route selection.' },
    { id: 'batch', label: 'Batch inference', description: 'Batch jobs.' },
    { id: 'alerts', label: 'Alerts', description: 'Backend alerts.' },
    { id: 'future', label: 'Future things', description: 'Added by a later backend.' },
  ],
  entries: [
    entry({
      key: 'MODEL_CATALOG',
      category: 'models',
      type: 'text',
      value: '{"models": []}',
      description: 'Model catalog as JSON.',
      one_time: true,
      restart_required: true,
    }),
    entry({
      key: 'SITE_BRANDING',
      category: 'application',
      type: 'text',
      value: '{}',
      description: 'Public branding as JSON.',
      one_time: true,
    }),
    entry({ key: 'SITE_NAME', category: 'general', value: 'Gateway', one_time: true }),
    entry({ key: 'LOG_LEVEL', category: 'general', value: 'INFO' }),
    entry({ key: 'COOKIE_DOMAIN', category: 'security', one_time: true }),
    entry({ key: 'LOGIN_RATE_LIMIT_PER_15MIN', category: 'security', type: 'int', value: 20 }),
    entry({ key: 'SESSION_HOURS', category: 'security', type: 'int', value: 12 }),
    entry({ key: 'SIGNUP_NOTIFY_EMAILS', category: 'signup', value: 'ops@example.test' }),
    entry({ key: 'SIGNUP_RATE_LIMIT_PER_HOUR', category: 'signup', type: 'int', value: 10 }),
    entry({
      key: 'SMTP_HOST',
      category: 'email',
      required: true,
      missing: true,
      is_set: false,
      value: null,
      source: 'default',
      one_time: true,
    }),
    entry({
      key: 'TRUST_PROXY_HEADERS',
      category: 'network',
      type: 'bool',
      value: false,
      one_time: true,
    }),
    entry({
      key: 'ZAI_API_KEY',
      category: 'providers',
      secret: true,
      required: true,
      missing: true,
      is_set: false,
      value: null,
      source: 'default',
      used_by: ['glm-4.6'],
    }),
    entry({
      key: 'CHUTES_BASE_URL',
      category: 'providers',
      value: 'https://llm.chutes.ai/v1',
      one_time: true,
    }),
    entry({ key: 'ENABLE_ROUTEWISE', category: 'routing', type: 'bool', value: false }),
    entry({ key: 'REQUEST_TIMEOUT_SECONDS', category: 'routing', type: 'float', value: 120 }),
    entry({ key: 'BATCH_RETENTION_DAYS', category: 'batch', type: 'int', value: 7 }),
    entry({ key: 'BATCH_MAX_REQUESTS', category: 'batch', type: 'int', value: 50000 }),
    entry({ key: 'ALERTS_ENABLED', category: 'alerts', type: 'bool', value: true }),
    webhook('SLACK_ALERTS_WEBHOOK_URL'),
    webhook('WECOM_ALERTS_WEBHOOK_URL'),
    webhook('SLACK_WEBHOOK_URL'),
    entry({ key: 'DEPLOYMENT_ENV', category: 'alerts', value: 'staging', one_time: true }),
    entry({
      key: 'FAILED_REQUEST_ALERT_THRESHOLD',
      category: 'alerts',
      type: 'int',
      value: 200,
    }),
    entry({ key: 'FUTURE_KNOB', category: 'future', value: 'x' }),
  ],
  missing: ['SMTP_HOST', 'ZAI_API_KEY'],
  pending_restart: [],
  restart_supported: true,
};

/** CONFIG, with `key` changed as `overrides` say. */
function configWith(key: string, overrides: Partial<ConfigEntry>): ConfigResponse {
  return {
    ...CONFIG,
    entries: CONFIG.entries.map((item) => (item.key === key ? { ...item, ...overrides } : item)),
  };
}

function runtime(key: string, value: boolean | number): RuntimeSettingItem {
  return {
    key,
    value,
    value_type: typeof value === 'boolean' ? 'bool' : 'int',
    default_value: value,
    description: key,
    min: null,
    max: null,
  };
}

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  getConfig: vi.fn(async () => CONFIG),
  patchConfig: vi.fn(),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // Every section reads on mount, whatever sub-tab is open; unmocked they
    // would reach the real gateway (see vitest.setup.ts).
    getAlertSnooze: vi.fn(async () => ({
      snoozed: false,
      snooze_until: null,
      seconds_remaining: 0,
    })),
    listAlertMutes: vi.fn(async () => ({ types: [] })),
    getUsageInsightsSettings: vi.fn(async () => ({
      configured: false,
      api_key_hint: null,
      model: 'gpt-4o-mini',
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listModelVisibility: vi.fn(async () => ({ models: [] })),
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listProviderRoutes: vi.fn(async () => ({ provider_options: [], routes: [] })),
    listBatchModelLimits: vi.fn(async () => ({ default_concurrency: 4, models: [] })),
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        runtime('log_full_payload', false),
        runtime('signup_enabled', true),
        runtime('auto_mode_enabled', false),
        runtime('analytics_token_usage_enabled', true),
        runtime('analytics_users_and_requests_enabled', true),
        runtime('analytics_performance_metrics_enabled', true),
        runtime('analytics_growth_rate_enabled', false),
        runtime('user_concurrency_free', 3),
        runtime('user_daily_quota_free', 100),
        // A setting no group names, from a later backend.
        runtime('mystery_knob', 5),
      ],
    })),
    updateRuntimeSetting: vi.fn(),
  };
});

import { getConfig, patchConfig } from '@/lib/api/config';
import {
  listModelConcurrency,
  listModelVisibility,
  listProviderRoutes,
  listRuntimeSettings,
  updateRuntimeSetting,
} from '@/lib/api/admin';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** The tabs listed with this configuration, in order, named as a screen reader hears them. */
const ALL_TABS = [
  'Overview',
  'Sign-up',
  'Sign-in & security',
  'Usage limits',
  'Providers, 1 missing',
  'Routing',
  'Batches',
  'Alerts',
  'Logging & privacy',
  'Analytics',
  'General',
  'Email, 1 missing',
  'Network',
  'Backup & restore',
  'Other',
];

/** The sub-tabs are these, in this order, named as a screen reader hears them. */
function expectTabs(names: string[]) {
  const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
  const tabs = within(tablist).getAllByRole('tab');
  expect(tabs.map((tab) => tab.textContent)).toHaveLength(names.length);
  names.forEach((name, index) =>
    expect(within(tablist).getByRole('tab', { name })).toBe(tabs[index]),
  );
}

/** The tab chosen now, by the name it is shown under. */
function expectSelected(name: string | RegExp) {
  expect(screen.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true');
}

/** The sections of the sub-tab on screen, top to bottom. */
function sectionsShown() {
  return within(screen.getByRole('tabpanel'))
    .queryAllByRole('region')
    .map((region) => region.getAttribute('aria-labelledby'))
    .map((id) => document.getElementById(id!)?.textContent);
}

/** A category's rows, top to bottom: each setting by its variable, and the heading of those set once. */
function rowsOf(category: string) {
  return Array.from(
    screen
      .getByRole('region', { name: category })
      .querySelectorAll<HTMLElement>('[data-config-key], h3'),
  ).map((row) => row.dataset.configKey ?? row.textContent);
}

/** The Advanced toggle of the tab on screen. */
function advancedToggle() {
  return screen.getByRole('button', { name: 'Advanced settings' });
}

/** Open the Advanced of the tab on screen. */
function openAdvanced() {
  const toggle = advancedToggle();
  if (toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
}

/** Render, and wait for the runtime settings, which arrive after the configuration. */
async function renderLoaded(...args: Parameters<typeof renderSettings>) {
  await renderSettings(...args);
  await screen.findByLabelText('mystery_knob value');
}

describe('Settings section links', () => {
  const scrollIntoView = vi.fn();
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');

  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
    if (originalScroll) {
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScroll);
    } else {
      delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    }
  });

  /** The link was followed: the page scrolled once, to `target`. */
  async function expectScrolledTo(target: () => HTMLElement) {
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: 'start' });
    expect(scrollIntoView.mock.contexts[0]).toBe(target());
  }

  it.each(['configuration', 'runtime settings'] as const)(
    'waits for both reads when %s arrives first, then scrolls only once',
    async (first) => {
      window.history.replaceState(
        null,
        '',
        '/dashboard/admin/settings?tab=analytics#settings-analytics',
      );
      let resolveConfig!: (value: ConfigResponse) => void;
      let resolveRuntime!: (value: { settings: RuntimeSettingItem[] }) => void;
      vi.mocked(getConfig).mockReturnValueOnce(
        new Promise((resolve) => {
          resolveConfig = resolve;
        }),
      );
      vi.mocked(listRuntimeSettings).mockReturnValueOnce(
        new Promise((resolve) => {
          resolveRuntime = resolve;
        }),
      );
      await renderSettings();

      const finishConfig = () => resolveConfig(CONFIG);
      const finishRuntime = () =>
        resolveRuntime({
          settings: [runtime('analytics_growth_rate_enabled', false)],
        });
      await act(async () => {
        (first === 'configuration' ? finishConfig : finishRuntime)();
      });
      expect(scrollIntoView).not.toHaveBeenCalled();
      await act(async () => {
        (first === 'configuration' ? finishRuntime : finishConfig)();
      });

      // Analytics has a tab of its own: the link opened it.
      const section = screen.getByRole('region', { name: 'Analytics' });
      expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: 'start' });
      expect(scrollIntoView.mock.contexts[0]).toBe(section);
      expectSelected('Analytics');

      vi.mocked(updateRuntimeSetting).mockResolvedValueOnce(
        runtime('analytics_growth_rate_enabled', true),
      );
      fireEvent.click(within(section).getByRole('switch', { name: 'Toggle Growth Rate' }));
      fireEvent.click(within(section).getByRole('button', { name: 'Save changes to Analytics' }));
      await waitFor(() =>
        expect(
          within(section).queryByRole('button', { name: 'Save changes to Analytics' }),
        ).not.toBeInTheDocument(),
      );
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ['', {}],
    ['#settings-missing', {}],
    ['#settings-analytics', { initialMissingOnly: true }],
  ])('does not scroll for an absent or hidden target (%s, %j)', async (hash, props) => {
    window.history.replaceState(null, '', `/dashboard/admin/settings${hash}`);
    await renderLoaded(undefined, props);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('opens the tab that holds a linked section, and its Advanced, then scrolls to it', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings#config-general-logging');
    await renderLoaded();

    // The server log settings are under the Advanced of Logging & privacy.
    await expectScrolledTo(() => screen.getByRole('region', { name: 'Server log' }));
    expectSelected('Logging & privacy');
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
    expect(replaceMock).toHaveBeenCalledWith(
      '/dashboard/admin/settings?tab=logging#config-general-logging',
      { scroll: false },
    );
  });

  it('keeps the hash when the linked section is on the default tab', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings#settings-all-tabs');
    await renderLoaded(undefined, { initialTab: 'alerts' });

    await expectScrolledTo(() => screen.getByRole('region', { name: 'All settings' }));
    expectSelected('Overview');
    expect(replaceMock).toHaveBeenCalledWith('/dashboard/admin/settings#settings-all-tabs', {
      scroll: false,
    });
  });

  it.each([
    // A setting by its key, a category's tuning and a part of another category.
    ['REQUEST_TIMEOUT_SECONDS', 'Routing', '[data-config-key="REQUEST_TIMEOUT_SECONDS"]'],
    ['LOG_LEVEL', 'Logging & privacy', '[data-config-key="LOG_LEVEL"]'],
    // A whole section, by its anchor: a document on the tab it configures.
    ['config-application-branding', 'General', '#config-application-branding'],
  ])('opens Advanced for a link to %s under it, and scrolls there', async (hash, tab, selector) => {
    window.history.replaceState(null, '', `/dashboard/admin/settings#${hash}`);
    await renderLoaded();

    await expectScrolledTo(() => document.querySelector<HTMLElement>(selector)!);
    expectSelected(tab);
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector(selector)).toBeVisible();
  });

  it('opens a tab with no Advanced for a link to a runtime setting on it', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings#log_full_payload');
    await renderLoaded();

    await expectScrolledTo(
      () => document.querySelector<HTMLElement>('[data-runtime-key="log_full_payload"]')!,
    );
    expectSelected('Logging & privacy');
    // At the top of the tab: Advanced stays as it was.
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it('follows a link to a section under Advanced once the page is open', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings');
    await renderLoaded();
    expect(scrollIntoView).not.toHaveBeenCalled();

    act(() => {
      window.history.replaceState(null, '', '/dashboard/admin/settings#config-routing-advanced');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    await expectScrolledTo(() => screen.getByRole('region', { name: 'Routing (advanced)' }));
    expectSelected('Routing');
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
  });

  it('opens the tab an Overview line names, at its section', async () => {
    await renderLoaded();

    // Alerts run, but no channel is chosen: the line says so, and opens the
    // tab at the section where the channel is chosen, below the snooze.
    const alerts = document.querySelector<HTMLElement>('[data-overview-item="alerts"]')!;
    expect(alerts).toHaveTextContent('No channel');
    fireEvent.click(within(alerts).getByRole('button', { name: 'Open Alerts' }));

    await expectScrolledTo(() => screen.getByRole('region', { name: 'Alerts' }));
    expectSelected('Alerts');
    // The address says where, as the link's would: a reload or a share lands there too.
    expect(replaceMock).toHaveBeenLastCalledWith(
      '/dashboard/admin/settings?tab=alerts#config-alerts',
      { scroll: false },
    );
  });
});

describe('Admin → Settings sub-tabs', () => {
  it('opens on the Overview, with every tab that has something to show under its heading', async () => {
    await renderLoaded();

    // Integrations has nothing on this gateway, so it is not listed.
    expectTabs(ALL_TABS);
    expectSelected('Overview');
    // The headings group the tabs for the eye; a screen reader hears the tabs alone.
    const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
    for (const heading of ['Access', 'Inference', 'Monitoring', 'Deployment']) {
      expect(within(tablist).getByText(heading)).toHaveAttribute('aria-hidden', 'true');
    }
    expect(screen.getByText(/^What is set up, what still needs a value/)).toBeVisible();
    // The page says at a glance that two required settings are missing and nothing is unsaved.
    expect(screen.getByText('2 required settings missing')).toBeVisible();
    expect(screen.getByText('No unsaved changes')).toBeVisible();
    expect(sectionsShown()).toEqual(['Setup status', 'All settings']);
  });

  it('says on the Overview how each thing stands, and opens the tab that changes it', async () => {
    await renderLoaded();

    const lines = Array.from(document.querySelectorAll<HTMLElement>('[data-overview-item]'));
    expect(lines.map((line) => line.dataset.overviewItem)).toEqual(['site', 'signup', 'alerts']);
    expect(lines[0]).toHaveTextContent('The site is named Gateway.');
    expect(lines[1]).toHaveTextContent('New users may sign up.');
    expect(screen.getByText('1 thing needs a look.')).toBeVisible();

    // Every other tab is listed with what it holds, and opens on a click.
    const all = within(screen.getByRole('region', { name: 'All settings' }));
    expect(all.getAllByRole('button')).toHaveLength(ALL_TABS.length - 1);
    fireEvent.click(all.getByRole('button', { name: /^Batches/ }));
    expectSelected('Batches');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings?tab=batches', {
      scroll: false,
    });
  });

  it.each([
    [
      'Sign-up',
      // The switches sit with the sign-up configuration of the same topic.
      ['Sign-up Controls', 'Signup Policy', 'Sign-up'],
      ['Sign-up (advanced)'],
    ],
    ['Sign-in & security', ['Security'], ['Security (advanced)']],
    ['Providers', ['Providers'], ['Providers (advanced)']],
    // RouteWise, with no RouteWise model, shows its empty state: no section of its own.
    ['Routing', ['Routing', 'Request Handling'], ['Routing (advanced)']],
    ['Batches', ['Batch inference', 'Batch Concurrency'], ['Batch inference (advanced)']],
    [
      'Alerts',
      // Snoozing first; the channel next; the long list of types to mute last.
      ['Alert Notifications', 'Alerts', 'Alert Types'],
      ['Alerts (advanced)'],
    ],
    ['Logging & privacy', ['Request Logging'], ['Server log']],
    // A document is shown on the tab it configures, under its own name.
    ['General', ['General'], ['Branding']],
  ])(
    'opens %s on what is commonly changed, and keeps the rest under Advanced, collapsed',
    async (tab, top, advanced) => {
      await renderLoaded(new RegExp(`^${tab}`));

      expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
      expect(sectionsShown()).toEqual(top);
      openAdvanced();
      expect(sectionsShown()).toEqual([...top, ...advanced]);
    },
  );

  it.each([
    ['Usage limits', ['Per-User Concurrency', 'Daily Quotas']],
    ['Analytics', ['Analytics', 'Usage Insights']],
    ['Email', ['Email']],
    ['Network', ['Network']],
    ['Backup & restore', ['Backup & Restore']],
    // A category no tab names, and a runtime setting no group takes.
    ['Other', ['Future things', 'Other Runtime Settings']],
  ])('shows all of %s at once, with nothing under Advanced', async (tab, sections) => {
    await renderLoaded(new RegExp(`^${tab}`));

    expect(sectionsShown()).toEqual(sections);
    expect(screen.queryByRole('button', { name: 'Advanced settings' })).not.toBeInTheDocument();
  });

  it('opens the Overview, and says so in the address, for a tab this gateway does not list', async () => {
    // Nothing for Other: no category no tab names, and no runtime setting no group takes.
    vi.mocked(getConfig).mockResolvedValueOnce({
      ...CONFIG,
      entries: CONFIG.entries.filter((item) => item.category !== 'future'),
    });
    let finishRuntime!: (value: { settings: RuntimeSettingItem[] }) => void;
    vi.mocked(listRuntimeSettings).mockReturnValueOnce(
      new Promise((resolve) => {
        finishRuntime = resolve;
      }),
    );
    await renderSettings(undefined, { initialTab: 'other' });

    // Its runtime settings may be on their way: the tab it names waits, listed.
    await screen.findByRole('tab', { name: 'Other' });
    expectSelected('Other');
    expect(replaceMock).not.toHaveBeenCalled();

    await act(async () => {
      finishRuntime({ settings: [runtime('log_full_payload', false)] });
    });
    expect(screen.queryByRole('tab', { name: 'Other' })).not.toBeInTheDocument();
    expectSelected('Overview');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
  });

  it('keeps the tab the address names once its runtime settings arrive', async () => {
    let finishRuntime!: (value: { settings: RuntimeSettingItem[] }) => void;
    vi.mocked(getConfig).mockResolvedValueOnce({
      ...CONFIG,
      entries: CONFIG.entries.filter((item) => item.category !== 'future'),
    });
    vi.mocked(listRuntimeSettings).mockReturnValueOnce(
      new Promise((resolve) => {
        finishRuntime = resolve;
      }),
    );
    await renderSettings(undefined, { initialTab: 'other' });
    await screen.findByRole('tab', { name: 'Other' });

    await act(async () => {
      finishRuntime({ settings: [runtime('mystery_knob', 5)] });
    });
    expectSelected('Other');
    expect(screen.getByLabelText('mystery_knob value')).toBeVisible();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('moves between tabs with the keyboard, and opens one on Enter or a click', async () => {
    await renderLoaded();
    const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
    const tabs = within(tablist).getAllByRole('tab');
    // One stop in the tab order: the chosen tab.
    expect(tabs.map((tab) => tab.tabIndex)).toEqual(tabs.map((_, index) => (index === 0 ? 0 : -1)));

    tabs[0].focus();
    fireEvent.keyDown(tabs[0], { key: 'ArrowDown' });
    expect(tabs[1]).toHaveFocus();
    fireEvent.keyDown(tabs[1], { key: 'ArrowRight' });
    expect(tabs[2]).toHaveFocus();
    fireEvent.keyDown(tabs[2], { key: 'End' });
    expect(tabs[tabs.length - 1]).toHaveFocus();
    // Past either end, around to the other.
    fireEvent.keyDown(tabs[tabs.length - 1], { key: 'ArrowDown' });
    expect(tabs[0]).toHaveFocus();
    fireEvent.keyDown(tabs[0], { key: 'ArrowUp' });
    expect(tabs[tabs.length - 1]).toHaveFocus();
    fireEvent.keyDown(tabs[tabs.length - 1], { key: 'Home' });
    expect(tabs[0]).toHaveFocus();

    // Moving opens nothing; the tab with focus opens as a button does.
    fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' });
    expectSelected('Overview');
    expect(replaceMock).not.toHaveBeenCalled();
    fireEvent.click(tabs[tabs.length - 1]);
    expectSelected('Other');
    expect(tabs[tabs.length - 1].tabIndex).toBe(0);
    expect(tabs[0].tabIndex).toBe(-1);
  });

  it('shows a note beside a tab only while it has something to note', async () => {
    await renderLoaded('Routing');
    const routing = screen.getByRole('tab', { name: 'Routing' });
    expect(routing.querySelector('[aria-hidden="true"]')).toBeNull();

    openAdvanced();
    fireEvent.change(screen.getByLabelText('Request timeout (seconds)'), {
      target: { value: '90' },
    });
    const changed = screen.getByRole('tab', { name: 'Routing, unsaved changes' });
    expect(changed.querySelector('[aria-hidden="true"]')).not.toBeNull();
  });

  it('heads each tab with its name and what it holds', async () => {
    await renderLoaded('Usage limits');
    const panel = screen.getByRole('tabpanel', { name: 'Usage limits' });
    expect(within(panel).getByText('Usage limits')).toBeVisible();
    expect(
      within(panel).getByText(/^How many requests one user may have in flight, and how much/),
    ).toBeVisible();
  });

  it('describes a section by what this page shows of it', async () => {
    await renderLoaded('General');
    // The category speaks of logging too, which this page shows on Logging & privacy.
    expect(
      within(screen.getByRole('region', { name: 'General' })).getByText(
        'Public addresses and site identity.',
      ),
    ).toBeVisible();
  });

  it('shows a built-in setting under a plain name, with its variable beside it', async () => {
    await renderLoaded('Sign-up');

    const row = document.querySelector<HTMLElement>('[data-config-key="SIGNUP_NOTIFY_EMAILS"]')!;
    expect(within(row).getByLabelText('New-account notification emails')).toHaveValue(
      'ops@example.test',
    );
    expect(within(row).getByText('SIGNUP_NOTIFY_EMAILS')).toBeVisible();

    // A setting this console has no name for keeps its variable's.
    await openSettingsTab('Other');
    expect(screen.getByLabelText('FUTURE_KNOB')).toHaveValue('x');
  });

  it('keeps Advanced collapsed until it is opened, says what it holds, and loads RouteWise only then', async () => {
    await renderLoaded('Routing');

    const toggle = advancedToggle();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText(/^Timeouts and the largest request/)).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Routing (advanced)' })).not.toBeInTheDocument();
    // Concealed, not removed: the browser's find in page still looks in it.
    const timeout = document.querySelector('[data-config-key="REQUEST_TIMEOUT_SECONDS"]');
    expect(timeout).not.toBeVisible();
    expect(timeout?.closest('[hidden]')).toHaveAttribute('hidden', 'until-found');
    // RouteWise reads nothing while its section is not on screen.
    expect(listProviderRoutes).not.toHaveBeenCalled();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(
      within(screen.getByRole('region', { name: 'Routing (advanced)' })).getByLabelText(
        'Request timeout (seconds)',
      ),
    ).toHaveValue(120);
    expect(await screen.findByText('No RouteWise models available.')).toBeVisible();
    expect(listProviderRoutes).toHaveBeenCalledTimes(1);

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'Routing (advanced)' })).not.toBeInTheDocument();
    expect(screen.getByText('No RouteWise models available.')).not.toBeVisible();

    // Each tab has its own Advanced, collapsed until it is opened.
    await openSettingsTab('Alerts');
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it('keeps a section with an unsaved change on screen under a collapsed Advanced, with its change', async () => {
    await renderLoaded('Routing');
    openAdvanced();
    fireEvent.change(screen.getByLabelText('Request timeout (seconds)'), {
      target: { value: '90' },
    });

    fireEvent.click(advancedToggle());
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    const routing = screen.getByRole('region', { name: 'Routing (advanced)' });
    expect(within(routing).getByLabelText('Request timeout (seconds)')).toHaveValue(90);
    expect(within(routing).getByText('1 unsaved change')).toBeVisible();
    expect(screen.getByText(/stays on screen\.$/)).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Routing, unsaved changes' })).toBeInTheDocument();

    // Another tab and back: the change is still there, and so is its section.
    await openSettingsTab('Alerts');
    await openSettingsTab(/^Routing/);
    expect(screen.getByLabelText('Request timeout (seconds)')).toHaveValue(90);

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Routing (advanced)' }));
    expect(screen.queryByRole('region', { name: 'Routing (advanced)' })).not.toBeInTheDocument();
    expect(screen.queryByText(/stays on screen\.$/)).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Routing' })).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('counts a change to a part of a category on the tab that shows the part', async () => {
    await renderLoaded('Logging & privacy');
    openAdvanced();
    fireEvent.change(screen.getByLabelText('Log level'), { target: { value: 'DEBUG' } });

    // LOG_LEVEL is a General setting, shown on Logging & privacy: that tab has the change.
    expect(
      screen.getByRole('tab', { name: 'Logging & privacy, unsaved changes' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'General' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Server log' }));
    expect(screen.getByRole('tab', { name: 'Logging & privacy' })).toBeInTheDocument();
  });

  it('keeps a section under Advanced on screen while a required value is missing', async () => {
    vi.mocked(getConfig).mockResolvedValueOnce({
      ...configWith('COOKIE_DOMAIN', {
        required: true,
        missing: true,
        is_set: false,
        value: null,
        source: 'default',
      }),
      missing: ['SMTP_HOST', 'ZAI_API_KEY', 'COOKIE_DOMAIN'],
    });
    await renderLoaded();

    await openSettingsTab('Sign-in & security, 1 missing');
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(rowsOf('Security (advanced)')).toEqual([
      'LOGIN_RATE_LIMIT_PER_15MIN',
      'Usually set once',
      'COOKIE_DOMAIN',
    ]);
    expect(screen.getByRole('region', { name: 'Security (advanced)' })).toBeVisible();
  });

  it('keeps a section under Advanced on screen while a stored value is refused', async () => {
    vi.mocked(getConfig).mockResolvedValueOnce(
      configWith('BATCH_MAX_REQUESTS', { invalid: 'must be at least 1' }),
    );
    await renderLoaded('Batches');

    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('region', { name: 'Batch inference (advanced)' })).toBeVisible();
  });

  it('opens Advanced when the browser finds a match in it', async () => {
    await renderLoaded('Routing');
    const concealed = document.getElementById('config-routing-advanced')!.closest('[hidden]')!;
    expect(concealed).toHaveAttribute('hidden', 'until-found');

    act(() => {
      concealed.dispatchEvent(new Event('beforematch'));
    });

    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('region', { name: 'Routing (advanced)' })).toBeVisible();
    expect(concealed).not.toHaveAttribute('hidden');
  });

  it('leaves the model configuration to Routing → Models: not listed, searched or read', async () => {
    await renderLoaded();

    expect(screen.queryByRole('tab', { name: /^Models/ })).not.toBeInTheDocument();
    // Not on any sub-tab, shown or hidden.
    expect(document.querySelector('[data-config-key="MODEL_CATALOG"]')).toBeNull();
    expect(screen.queryByText('Model catalog')).not.toBeInTheDocument();
    expect(screen.queryByText('Model Visibility')).not.toBeInTheDocument();
    expect(screen.queryByText('Model Concurrency Limit')).not.toBeInTheDocument();
    expect(listModelVisibility).not.toHaveBeenCalled();
    expect(listModelConcurrency).not.toHaveBeenCalled();

    // General keeps the site's documents, under Advanced; Providers says
    // where the catalog went.
    await openSettingsTab(/^General/);
    openAdvanced();
    expect(rowsOf('Branding')).toEqual(['SITE_BRANDING']);
    await openSettingsTab(/^Providers/);
    expect(
      within(screen.getByRole('tabpanel')).getByText(
        /The model catalog itself is under Routing → Models\.$/,
      ),
    ).toBeVisible();

    // A search looks through this page's categories only.
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'catalog' } });
    expect(screen.getByText(/0 settings match, from every tab/)).toBeInTheDocument();
    expect(screen.getByText('No settings match.')).toBeInTheDocument();
  });

  it.each([
    ['analytics_token_usage_enabled', 'Token Usage', true],
    ['analytics_users_and_requests_enabled', 'Users and Requests', true],
    ['analytics_performance_metrics_enabled', 'Performance Metrics', true],
    ['analytics_growth_rate_enabled', 'Growth Rate', false],
  ] as const)('saves the independent %s switch from Analytics', async (key, label, initial) => {
    await renderLoaded();
    await openSettingsTab('Analytics');
    const analytics = within(screen.getByRole('region', { name: 'Analytics' }));
    const toggle = analytics.getByRole('switch', { name: `Toggle ${label}` });
    expect(analytics.getByText(label)).toBeVisible();
    expect(analytics.getAllByRole('switch')).toHaveLength(4);
    expect(toggle).toHaveAttribute('aria-checked', String(initial));

    vi.mocked(updateRuntimeSetting).mockImplementation(async (settingKey, value) => ({
      ...runtime(settingKey, value as boolean),
      default_value: initial,
    }));
    for (const [index, enabled] of [!initial, initial].entries()) {
      fireEvent.click(toggle);
      expect(updateRuntimeSetting).toHaveBeenCalledTimes(index);
      fireEvent.click(analytics.getByRole('button', { name: 'Save changes to Analytics' }));
      await waitFor(() => expect(updateRuntimeSetting).toHaveBeenLastCalledWith(key, enabled));
      await waitFor(() =>
        expect(
          analytics.queryByRole('button', { name: 'Save changes to Analytics' }),
        ).not.toBeInTheDocument(),
      );
      expect(toggle).toHaveAttribute('aria-checked', String(enabled));
    }
  });

  it.each([
    [
      'General',
      // A section of nothing else needs no heading for them.
      { General: ['SITE_NAME'] },
      { Branding: ['SITE_BRANDING'] },
    ],
    [
      'Sign-in & security',
      { Security: ['SESSION_HOURS'] },
      {
        'Security (advanced)': ['LOGIN_RATE_LIMIT_PER_15MIN', 'Usually set once', 'COOKIE_DOMAIN'],
      },
    ],
    [
      'Alerts',
      // The alert channel goes with its webhooks, which are set once, at the top.
      { Alerts: ['ALERTS_ENABLED', 'Usually set once'] },
      {
        'Alerts (advanced)': [
          'FAILED_REQUEST_ALERT_THRESHOLD',
          'Usually set once',
          'DEPLOYMENT_ENV',
        ],
      },
    ],
  ])(
    'lists the settings usually set once after the others of their section, on %s',
    async (tab, top, advanced) => {
      await renderLoaded(new RegExp(`^${tab}`));
      for (const [section, rows] of Object.entries(top)) {
        expect(rowsOf(section), section).toEqual(rows);
      }
      openAdvanced();
      for (const [section, rows] of Object.entries(advanced)) {
        expect(rowsOf(section), section).toEqual(rows);
      }
    },
  );

  it('puts the alert channel under the heading of what is set once', async () => {
    await renderLoaded('Alerts');
    const alerts = screen.getByRole('region', { name: 'Alerts' });
    const heading = within(alerts).getByRole('heading', { name: 'Usually set once' });
    expect(heading.compareDocumentPosition(within(alerts).getByText('Notification channel'))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('keeps the order of a backend that does not say what is set once', async () => {
    vi.mocked(getConfig).mockResolvedValueOnce({
      ...CONFIG,
      entries: CONFIG.entries.map((item) => {
        const { one_time: _oneTime, ...rest } = item;
        return rest;
      }),
    });
    await renderLoaded('Alerts');

    const alerts = screen.getByRole('region', { name: 'Alerts' });
    expect(
      within(alerts)
        .getByText('Notification channel')
        .compareDocumentPosition(within(alerts).getByLabelText('Alert engine')),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    openAdvanced();
    expect(rowsOf('Alerts (advanced)')).toEqual([
      'DEPLOYMENT_ENV',
      'FAILED_REQUEST_ALERT_THRESHOLD',
    ]);
    await openSettingsTab(/^Sign-in/);
    openAdvanced();
    expect(rowsOf('Security (advanced)')).toEqual(['COOKIE_DOMAIN', 'LOGIN_RATE_LIMIT_PER_15MIN']);
  });

  it('opens the sub-tab a link names, and puts the one chosen in the address', async () => {
    await renderLoaded(undefined, { initialTab: 'alerts' });
    expectSelected('Alerts');
    expect(sectionsShown()).toEqual(['Alert Notifications', 'Alerts', 'Alert Types']);

    await openSettingsTab('Overview');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
    await openSettingsTab('Alerts');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings?tab=alerts', {
      scroll: false,
    });
  });

  it('asks before another admin tab while any sub-tab holds an unsaved change', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    window.history.replaceState(null, '', '/dashboard/admin/settings');
    // Another admin tab's link, as the tab bar above the page renders it.
    const models = document.body.appendChild(document.createElement('a'));
    models.href = '/dashboard/admin/routing/models';
    try {
      await renderLoaded(/^Sign-up/);
      fireEvent.click(models);
      expect(confirm).not.toHaveBeenCalled();

      fireEvent.change(screen.getByLabelText('New-account notification emails'), {
        target: { value: 'admins@example.test' },
      });
      // Shown or not, the sub-tab's change would be lost with the page.
      await openSettingsTab('Alerts');
      expect(fireEvent.click(models)).toBe(false);
      expect(confirm).toHaveBeenCalledTimes(1);

      await openSettingsTab(/^Sign-up/);
      fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
      fireEvent.click(models);
      expect(confirm).toHaveBeenCalledTimes(1);
    } finally {
      models.remove();
      confirm.mockRestore();
    }
  });

  it('keeps unsaved changes while another sub-tab is open, and says where they are', async () => {
    await renderLoaded(/^Sign-up/);
    expectSelected('Sign-up');

    fireEvent.change(screen.getByLabelText('New-account notification emails'), {
      target: { value: 'admins@example.test' },
    });
    expect(screen.getByRole('tab', { name: 'Sign-up, unsaved changes' })).toBeInTheDocument();
    expect(screen.getByText('1 unsaved change across all tabs')).toBeVisible();

    await openSettingsTab(/^Routing/);
    expect(screen.queryByLabelText('New-account notification emails')).not.toBeVisible();
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle auto mode enabled' }));
    expect(screen.getByRole('tab', { name: 'Routing, unsaved changes' })).toBeInTheDocument();

    await openSettingsTab('Alerts');
    // Still said on the tabs that are not on screen: a dot for the eye, words in the name.
    for (const name of ['Sign-up, unsaved changes', 'Routing, unsaved changes']) {
      expect(screen.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'false');
    }

    await openSettingsTab(/^Sign-up/);
    expect(screen.getByLabelText('New-account notification emails')).toHaveValue(
      'admins@example.test',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
    expect(screen.getByRole('tab', { name: 'Sign-up' })).toBeInTheDocument();

    await openSettingsTab(/^Routing/);
    expect(screen.getByRole('switch', { name: 'Toggle auto mode enabled' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Request Handling' }));
    expect(screen.getByRole('tab', { name: 'Routing' })).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('searches configuration and runtime settings on every tab, and hides the rest until cleared', async () => {
    await renderLoaded();

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'glm' } });
    // No sub-tab is chosen while a filter lists every one of them.
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    const providers = screen.getByRole('region', { name: 'Providers' });
    expect(providers).toHaveTextContent('Providers');
    expect(within(providers).getByLabelText('Z.ai API key')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'General' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Setup status' })).not.toBeInTheDocument();
    expect(screen.queryByText(/^What is set up, what still needs a value/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Search settings')).toHaveValue('');
    expectSelected('Overview');
    expect(sectionsShown()).toContain('Setup status');

    // A runtime setting is found by its words too, not only by its key.
    fireEvent.change(screen.getByLabelText('Search settings'), {
      target: { value: 'concurrency' },
    });
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    const concurrency = screen.getByRole('region', { name: 'Per-User Concurrency' });
    expect(within(concurrency).getByLabelText('user_concurrency_free value')).toBeInTheDocument();
    expect(concurrency).toHaveTextContent('Usage limits');

    // And a setting by the plain name it is shown under.
    fireEvent.change(screen.getByLabelText('Search settings'), {
      target: { value: 'notification emails' },
    });
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Sign-up' })).getByLabelText(
        'New-account notification emails',
      ),
    ).toBeInTheDocument();
  });

  it('finds settings under Advanced with a search, says where they live, and keeps a change made there', async () => {
    await renderLoaded();

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'timeout' } });
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    // No toggle while a filter lists every tab: what matches under Advanced is listed with the rest.
    expect(screen.queryByRole('button', { name: 'Advanced settings' })).not.toBeInTheDocument();
    const routing = screen.getByRole('region', { name: 'Routing (advanced)' });
    expect(routing).toHaveTextContent('Routing · Advanced');
    expect(screen.queryByRole('region', { name: 'Routing' })).not.toBeInTheDocument();
    fireEvent.change(within(routing).getByLabelText('Request timeout (seconds)'), {
      target: { value: '90' },
    });

    // A part of a category, shown on another tab, is tagged with that tab.
    fireEvent.change(screen.getByLabelText('Search settings'), {
      target: { value: 'log level' },
    });
    const serverLog = screen.getByRole('region', { name: 'Server log' });
    expect(serverLog).toHaveTextContent('Logging & privacy · Advanced');
    expect(within(serverLog).getByLabelText('Log level')).toBeVisible();

    // Cleared, the change waits under the collapsed Advanced, on screen.
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    await openSettingsTab(/^Routing/);
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(
      within(screen.getByRole('region', { name: 'Routing (advanced)' })).getByLabelText(
        'Request timeout (seconds)',
      ),
    ).toHaveValue(90);
  });

  it('opens on what is missing, from every sub-tab, when linked that way', async () => {
    await renderLoaded(undefined, { initialMissingOnly: true });

    expect(screen.getByRole('checkbox', { name: 'Missing only' })).toBeChecked();
    const email = screen.getByRole('region', { name: 'Email' });
    expect(email).toHaveTextContent('Email');
    expect(within(email).getByLabelText('SMTP host')).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Providers' })).getByLabelText('Z.ai API key'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Security' })).not.toBeInTheDocument();

    // Clearing drops the `?missing=1` the page was opened with.
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
  });

  it('keeps the runtime settings when the backend has no configuration API', async () => {
    vi.mocked(getConfig).mockRejectedValueOnce(new APIError('HTTP_404', 'Not Found', 404));
    await renderLoaded();

    expect(screen.getByRole('alert')).toHaveTextContent(
      'does not support database-backed configuration',
    );

    // Only the tabs with runtime settings or sections of their own are listed.
    expectTabs([
      'Overview',
      'Sign-up',
      'Usage limits',
      'Routing',
      'Batches',
      'Alerts',
      'Logging & privacy',
      'Analytics',
      'Other',
    ]);
    // The Overview says what the runtime settings tell it.
    expect(
      Array.from(document.querySelectorAll<HTMLElement>('[data-overview-item]')).map(
        (line) => line.dataset.overviewItem,
      ),
    ).toEqual(['signup']);
    expect(screen.queryByLabelText('Search settings')).not.toBeInTheDocument();
    await waitFor(() => expect(listRuntimeSettings).toHaveBeenCalledTimes(1));

    // A tab whose Advanced holds only configuration has nothing to open.
    await openSettingsTab('Sign-up');
    expect(sectionsShown()).toEqual(['Sign-up Controls', 'Signup Policy']);
    expect(screen.queryByRole('button', { name: 'Advanced settings' })).not.toBeInTheDocument();
  });
});
