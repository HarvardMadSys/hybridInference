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
 * One setting or more in each category a tab names, and one in a category
 * none names. Where a category is split, it has a setting at the top and one
 * under Advanced (`ADVANCED_SETTINGS`), and some are usually set once. The
 * keys the layout does not name (`SESSION_HOURS`, `FUTURE_KNOB`) stay at the
 * top. The model catalog's category is Routing → Models', which this page
 * leaves out.
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

/** The sub-tabs are these, in this order, named as a screen reader hears them. */
function expectTabs(names: string[]) {
  const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
  const tabs = within(tablist).getAllByRole('tab');
  expect(tabs).toHaveLength(names.length);
  names.forEach((name, index) =>
    expect(within(tablist).getByRole('tab', { name })).toBe(tabs[index]),
  );
}

/** The sections of the sub-tab on screen, top to bottom. */
function sectionsShown() {
  return within(screen.getByRole('tabpanel'))
    .queryAllByRole('region')
    .map((region) => region.getAttribute('aria-labelledby'))
    .map((id) => document.getElementById(id!)?.textContent);
}

/** A category's rows, top to bottom: each setting by its name, and the heading of those set once. */
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
        '/dashboard/admin/settings?tab=operations#settings-analytics',
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

      // Analytics is under the Advanced of Operations: the link opened both.
      const section = screen.getByRole('region', { name: 'Analytics' });
      expect(scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: 'start' });
      expect(scrollIntoView.mock.contexts[0]).toBe(section);

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
    window.history.replaceState(null, '', '/dashboard/admin/settings#settings-analytics');
    await renderLoaded();

    await expectScrolledTo(() => screen.getByRole('region', { name: 'Analytics' }));
    expect(screen.getByRole('tab', { name: 'Operations' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps the hash when the linked section is on the default tab', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings#settings-quotas');
    await renderLoaded(undefined, { initialTab: 'operations' });

    await expectScrolledTo(() => screen.getByRole('region', { name: 'Daily Quotas' }));
    expect(screen.getByRole('tab', { name: 'Access & Limits' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    // A section at the top opens nothing more.
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it.each([
    // A setting by its key, the configuration's and a runtime one's.
    [
      'REQUEST_TIMEOUT_SECONDS',
      'Providers & Routing',
      '[data-config-key="REQUEST_TIMEOUT_SECONDS"]',
    ],
    ['log_full_payload', 'Operations', '[data-runtime-key="log_full_payload"]'],
    // A whole category's section, by its anchor.
    ['config-application', 'Site & Setup', '#config-application'],
  ])('opens Advanced for a link to %s under it, and scrolls there', async (hash, tab, selector) => {
    window.history.replaceState(null, '', `/dashboard/admin/settings#${hash}`);
    await renderLoaded();

    await expectScrolledTo(() => document.querySelector<HTMLElement>(selector)!);
    expect(screen.getByRole('tab', { name: new RegExp(`^${tab}`) })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector(selector)).toBeVisible();
  });

  it('follows a link to a section under Advanced once the page is open', async () => {
    window.history.replaceState(null, '', '/dashboard/admin/settings');
    await renderLoaded();
    expect(scrollIntoView).not.toHaveBeenCalled();

    act(() => {
      window.history.replaceState(null, '', '/dashboard/admin/settings#settings-logging');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    await expectScrolledTo(() => screen.getByRole('region', { name: 'Request Logging' }));
    expect(screen.getByRole('tab', { name: 'Operations' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
  });
});

describe('Admin → Settings sub-tabs', () => {
  it('opens on Access & Limits, with one tab per task and backup last on Site & Setup', async () => {
    await renderLoaded();

    expectTabs([
      'Access & Limits',
      'Providers & Routing, 1 missing',

      'Operations',
      'Site & Setup, 1 missing',
    ]);
    expect(screen.getByRole('tab', { name: 'Access & Limits' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByText(/^Who may sign up or sign in/)).toBeVisible();
    // The page says at a glance that two required settings are missing and nothing is unsaved.
    expect(screen.getByText('2 required settings missing')).toBeVisible();
    expect(screen.getByText('No unsaved changes')).toBeVisible();

    // Each runtime setting is in its topic's group.
    expect(
      within(screen.getByRole('region', { name: 'Daily Quotas' })).getByRole('button', {
        name: 'Apply to existing users',
      }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Per-User Concurrency' })).getByLabelText(
        'user_concurrency_free value',
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [
      // The switches changed most often sit with the sign-up configuration of
      // the same topic: one tab answers who may use the gateway, and how much.
      'Access & Limits',
      [
        'Access Controls',
        'Per-User Concurrency',
        'Daily Quotas',
        'Signup Policy',
        'Sign-up',
        'Security',
      ],
      ['Sign-up (advanced)', 'Security (advanced)'],
    ],
    [
      'Providers & Routing',
      ['Providers', 'Routing', 'Request Handling'],
      // RouteWise, with no RouteWise model, shows its empty state: no section of its own.
      ['Providers (advanced)', 'Routing (advanced)'],
    ],
    [
      'Operations',
      [
        'Alert Notifications',
        'Alert Types',
        'Alerts',
        'Batch inference',
        'Batch Concurrency',
        'Other Runtime Settings',
      ],
      ['Alerts (advanced)', 'Batch inference (advanced)', 'Request Logging', 'Analytics'],
    ],
    [
      // A category no tab names still has a place, at the top; the catalog's
      // is not one. Backup & restore closes the everyday part of the tab.
      'Site & Setup',
      ['General', 'Email', 'Future things', 'Backup & Restore'],
      ['General (advanced)', 'Network', 'Application', 'Usage Insights'],
    ],
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

  it('keeps Advanced collapsed until it is opened, says what it holds, and loads RouteWise only then', async () => {
    await renderLoaded('Providers & Routing, 1 missing');

    const toggle = advancedToggle();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText(/^The defaults offered for new routes/)).toBeVisible();
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
        'REQUEST_TIMEOUT_SECONDS',
      ),
    ).toHaveValue(120);
    expect(await screen.findByText('No RouteWise models available.')).toBeVisible();
    expect(listProviderRoutes).toHaveBeenCalledTimes(1);

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'Routing (advanced)' })).not.toBeInTheDocument();
    expect(screen.getByText('No RouteWise models available.')).not.toBeVisible();

    // Each tab has its own Advanced, collapsed until it is opened.
    await openSettingsTab('Operations');
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it('keeps a section with an unsaved change on screen under a collapsed Advanced, with its change', async () => {
    await renderLoaded('Providers & Routing, 1 missing');
    openAdvanced();
    fireEvent.change(screen.getByLabelText('REQUEST_TIMEOUT_SECONDS'), {
      target: { value: '90' },
    });

    fireEvent.click(advancedToggle());
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    const routing = screen.getByRole('region', { name: 'Routing (advanced)' });
    expect(within(routing).getByLabelText('REQUEST_TIMEOUT_SECONDS')).toHaveValue(90);
    expect(within(routing).getByText('1 unsaved change')).toBeVisible();
    expect(screen.getByText(/stays on screen\.$/)).toBeVisible();
    // The sections with nothing to do are concealed again.
    expect(screen.queryByRole('region', { name: 'Providers (advanced)' })).not.toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: 'Providers & Routing, unsaved changes, 1 missing' }),
    ).toBeInTheDocument();

    // Another tab and back: the change is still there, and so is its section.
    await openSettingsTab('Operations');
    await openSettingsTab(/^Providers & Routing/);
    expect(screen.getByLabelText('REQUEST_TIMEOUT_SECONDS')).toHaveValue(90);

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Routing (advanced)' }));
    expect(screen.queryByRole('region', { name: 'Routing (advanced)' })).not.toBeInTheDocument();
    expect(screen.queryByText(/stays on screen\.$/)).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('keeps a runtime group with an unsaved change on screen under a collapsed Advanced', async () => {
    await renderLoaded('Operations');
    openAdvanced();
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle log full payload' }));
    fireEvent.click(advancedToggle());
    expect(screen.getByRole('switch', { name: 'Toggle log full payload' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('region', { name: 'Request Logging' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Analytics' })).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
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

    expect(screen.getByRole('tab', { name: 'Access & Limits, 1 missing' })).toBeInTheDocument();
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(rowsOf('Security (advanced)')).toEqual([
      'LOGIN_RATE_LIMIT_PER_15MIN',
      'Usually set once',
      'COOKIE_DOMAIN',
    ]);
    expect(screen.queryByRole('region', { name: 'Sign-up (advanced)' })).not.toBeInTheDocument();
  });

  it('keeps a section under Advanced on screen while a stored value is refused', async () => {
    vi.mocked(getConfig).mockResolvedValueOnce(
      configWith('BATCH_MAX_REQUESTS', { invalid: 'must be at least 1' }),
    );
    await renderLoaded('Operations');

    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('region', { name: 'Batch inference (advanced)' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Alerts (advanced)' })).not.toBeInTheDocument();
  });

  it('opens Advanced when the browser finds a match in it', async () => {
    await renderLoaded('Operations');
    const concealed = document.getElementById('settings-analytics')!.closest('[hidden]')!;
    expect(concealed).toHaveAttribute('hidden', 'until-found');

    act(() => {
      concealed.dispatchEvent(new Event('beforematch'));
    });

    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('region', { name: 'Analytics' })).toBeVisible();
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

    // Site & Setup keeps the other documents, under Advanced; Providers &
    // Routing says where the catalog went.
    await openSettingsTab(/^Site & Setup/);
    openAdvanced();
    expect(rowsOf('Application')).toEqual(['SITE_BRANDING']);
    await openSettingsTab(/^Providers & Routing/);
    expect(screen.getByText(/The model catalog itself is under Routing → Models\.$/)).toBeVisible();

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
  ] as const)(
    'saves the independent %s switch from the Advanced of Operations',
    async (key, label, initial) => {
      await renderLoaded();
      await openSettingsTab('Operations');
      openAdvanced();
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
    },
  );

  it.each([
    [
      'Site & Setup',
      // A section of nothing else needs no heading for them.
      { General: ['SITE_NAME'], Email: ['SMTP_HOST'] },
      { 'General (advanced)': ['LOG_LEVEL'], Network: ['TRUST_PROXY_HEADERS'] },
    ],
    [
      'Access & Limits',
      { Security: ['SESSION_HOURS'], 'Sign-up': ['SIGNUP_NOTIFY_EMAILS'] },
      {
        'Security (advanced)': ['LOGIN_RATE_LIMIT_PER_15MIN', 'Usually set once', 'COOKIE_DOMAIN'],
      },
    ],
    [
      'Operations',
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
    await renderLoaded('Operations');
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
    await renderLoaded('Operations');

    const alerts = screen.getByRole('region', { name: 'Alerts' });
    expect(
      within(alerts)
        .getByText('Notification channel')
        .compareDocumentPosition(within(alerts).getByLabelText('ALERTS_ENABLED')),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    openAdvanced();
    expect(rowsOf('Alerts (advanced)')).toEqual([
      'DEPLOYMENT_ENV',
      'FAILED_REQUEST_ALERT_THRESHOLD',
    ]);
    await openSettingsTab(/^Access/);
    openAdvanced();
    expect(rowsOf('Security (advanced)')).toEqual(['COOKIE_DOMAIN', 'LOGIN_RATE_LIMIT_PER_15MIN']);
  });

  it('opens the sub-tab a link names, and puts the one chosen in the address', async () => {
    await renderLoaded(undefined, { initialTab: 'operations' });
    expect(screen.getByRole('tab', { name: 'Operations' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(sectionsShown()).toEqual([
      'Alert Notifications',
      'Alert Types',
      'Alerts',
      'Batch inference',
      'Batch Concurrency',
      'Other Runtime Settings',
    ]);

    await openSettingsTab(/^Access/);
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
    await openSettingsTab('Operations');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings?tab=operations', {
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
      await renderLoaded(/^Access/);
      fireEvent.click(models);
      expect(confirm).not.toHaveBeenCalled();

      fireEvent.change(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS'), {
        target: { value: 'admins@example.test' },
      });
      // Shown or not, the sub-tab's change would be lost with the page.
      await openSettingsTab('Operations');
      expect(fireEvent.click(models)).toBe(false);
      expect(confirm).toHaveBeenCalledTimes(1);

      await openSettingsTab(/^Access/);
      fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
      fireEvent.click(models);
      expect(confirm).toHaveBeenCalledTimes(1);
    } finally {
      models.remove();
      confirm.mockRestore();
    }
  });

  it('keeps unsaved changes while another sub-tab is open, and says where they are', async () => {
    await renderLoaded(/^Access/);
    expect(screen.getByRole('tab', { name: 'Access & Limits' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    fireEvent.change(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS'), {
      target: { value: 'admins@example.test' },
    });
    expect(
      screen.getByRole('tab', { name: 'Access & Limits, unsaved changes' }),
    ).toBeInTheDocument();
    expect(screen.getByText('1 unsaved change across all tabs')).toBeVisible();

    await openSettingsTab(/^Providers & Routing/);
    expect(screen.queryByLabelText('SIGNUP_NOTIFY_EMAILS')).not.toBeVisible();
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle auto mode enabled' }));
    expect(
      screen.getByRole('tab', { name: /Providers & Routing.*unsaved changes/ }),
    ).toBeInTheDocument();

    await openSettingsTab('Operations');
    // Still said on the tabs that are not on screen.
    expect(screen.getByRole('tab', { name: /^Access/ })).toHaveTextContent('unsaved changes');
    expect(screen.getByRole('tab', { name: /^Providers/ })).toHaveTextContent('unsaved changes');

    await openSettingsTab(/^Access/);
    expect(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS')).toHaveValue('admins@example.test');
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
    expect(screen.getByRole('tab', { name: 'Access & Limits' })).toBeInTheDocument();

    await openSettingsTab(/^Providers & Routing/);
    expect(screen.getByRole('switch', { name: 'Toggle auto mode enabled' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Request Handling' }));
    expect(screen.getByRole('tab', { name: 'Providers & Routing, 1 missing' })).toBeInTheDocument();
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
    expect(providers).toHaveTextContent('Providers & Routing');
    expect(within(providers).getByLabelText('ZAI_API_KEY')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'General' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Access Controls' })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Who may sign up or sign in/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Search settings')).toHaveValue('');
    expect(screen.getByRole('tab', { name: 'Access & Limits' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(sectionsShown()).toContain('Access Controls');

    // A runtime setting is found by its words too, not only by its key.
    fireEvent.change(screen.getByLabelText('Search settings'), {
      target: { value: 'concurrency' },
    });
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    const concurrency = screen.getByRole('region', { name: 'Per-User Concurrency' });
    expect(within(concurrency).getByLabelText('user_concurrency_free value')).toBeInTheDocument();
    expect(concurrency).toHaveTextContent('Access & Limits');
  });

  it('finds settings under Advanced with a search, says where they live, and keeps a change made there', async () => {
    await renderLoaded();

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'timeout' } });
    expect(screen.getByText(/1 setting matches, from every tab/)).toBeInTheDocument();
    // No toggle while a filter lists every tab: what matches under Advanced is listed with the rest.
    expect(screen.queryByRole('button', { name: 'Advanced settings' })).not.toBeInTheDocument();
    const routing = screen.getByRole('region', { name: 'Routing (advanced)' });
    expect(routing).toHaveTextContent('Providers & Routing · Advanced');
    expect(screen.queryByRole('region', { name: 'Routing' })).not.toBeInTheDocument();
    fireEvent.change(within(routing).getByLabelText('REQUEST_TIMEOUT_SECONDS'), {
      target: { value: '90' },
    });

    // A runtime setting under Advanced is found, and tagged, the same way.
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'payload' } });
    const logging = screen.getByRole('region', { name: 'Request Logging' });
    expect(logging).toHaveTextContent('Operations · Advanced');
    expect(within(logging).getByRole('switch', { name: 'Toggle log full payload' })).toBeVisible();

    // Cleared, the change waits under the collapsed Advanced, on screen.
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    await openSettingsTab(/^Providers & Routing/);
    expect(advancedToggle()).toHaveAttribute('aria-expanded', 'false');
    expect(
      within(screen.getByRole('region', { name: 'Routing (advanced)' })).getByLabelText(
        'REQUEST_TIMEOUT_SECONDS',
      ),
    ).toHaveValue(90);
    expect(screen.queryByRole('region', { name: 'Providers (advanced)' })).not.toBeInTheDocument();
  });

  it('opens on what is missing, from every sub-tab, when linked that way', async () => {
    await renderLoaded(undefined, { initialMissingOnly: true });

    expect(screen.getByRole('checkbox', { name: 'Missing only' })).toBeChecked();
    const email = screen.getByRole('region', { name: 'Email' });
    expect(email).toHaveTextContent('Site & Setup');
    expect(within(email).getByLabelText('SMTP_HOST')).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Providers' })).getByLabelText('ZAI_API_KEY'),
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

    // Every tab still has its runtime settings or its own sections to show.
    expectTabs(['Access & Limits', 'Providers & Routing', 'Operations', 'Site & Setup']);
    expect(sectionsShown()).toEqual([
      'Access Controls',
      'Per-User Concurrency',
      'Daily Quotas',
      'Signup Policy',
    ]);
    // Its Advanced holds only configuration: there is nothing to open.
    expect(screen.queryByRole('button', { name: 'Advanced settings' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Search settings')).not.toBeInTheDocument();
    await waitFor(() => expect(listRuntimeSettings).toHaveBeenCalledTimes(1));

    // Site & Setup keeps its backup at the top, and Usage Insights under its Advanced.
    await openSettingsTab('Site & Setup');
    expect(sectionsShown()).toEqual(['Backup & Restore']);
    openAdvanced();
    expect(sectionsShown()).toEqual(['Backup & Restore', 'Usage Insights']);
  });
});
