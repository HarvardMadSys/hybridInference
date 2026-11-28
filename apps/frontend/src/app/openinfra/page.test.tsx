// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { Catalog } from '@/lib/i18n/translate';
import zhCN from '@/lib/i18n/messages/zh-CN.json';
import OpenInfraPage from './page';
import { OpenInfraContent } from './OpenInfraContent';
import { dashboard, history } from './testFixtures';

const api = vi.hoisted(() => ({
  getOpenInfraDashboard: vi.fn(),
  getOpenInfraHistory: vi.fn(),
  exportOpenInfra: vi.fn(),
}));
vi.mock('@/lib/api/openinfra', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/openinfra')>()),
  ...api,
}));

const auth = vi.hoisted(() => ({
  state: { isAuthenticated: false, user: null as null | { is_admin: boolean } },
}));
vi.mock('@/components/providers', () => ({ useAuth: () => ({ state: auth.state }) }));

const site = vi.hoisted(() => ({ openinfraUrl: '' }));
vi.mock('@/config/site-config.server', () => ({
  loadRuntimeSiteConfig: async () => ({ openinfraUrl: site.openinfraUrl }),
}));
const redirect = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  }),
);
vi.mock('next/navigation', () => ({ redirect }));

const saved = vi.hoisted(() => [] as Array<{ name: string; mime: string }>);
vi.mock('./download', () => ({
  saveFile: (name: string, _content: unknown, mime: string) => saved.push({ name, mime }),
}));

function ok<T>(data: T) {
  return { status: 'ok' as const, data };
}

async function loaded() {
  return screen.findByRole('heading', { name: 'Served' });
}

beforeEach(() => {
  window.history.replaceState(null, '', '/openinfra');
  api.getOpenInfraDashboard.mockReset().mockResolvedValue(ok(dashboard()));
  api.getOpenInfraHistory.mockReset().mockResolvedValue(ok(history()));
  api.exportOpenInfra.mockReset();
  auth.state = { isAuthenticated: false, user: null };
  saved.length = 0;
  // jsdom has no canvas; the charts draw nothing and keep their labels.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('OpenInfraPage', () => {
  it('shows the first deployment, one tab per name, with every section', async () => {
    render(<OpenInfraContent />);
    expect(screen.getByText('Loading OpenInfra…')).toBeInTheDocument();
    await loaded();

    const tabs = within(screen.getByRole('navigation', { name: 'Deployments' })).getAllByRole(
      'button',
    );
    // The offline row left under chat-model's name gets no tab of its own.
    expect(tabs.map((tab) => tab.textContent)).toEqual(['chat-model', 'code-model', 'embed-model']);
    expect(tabs[0]).toHaveAttribute('aria-pressed', 'true');
    // The address now names it, so it can be shared. An effect writes it,
    // after the render that shows the page.
    await waitFor(() => expect(window.location.hash).toBe('#chat-model'));
    expect(screen.getByText('chat-model · GPU 0,1 · node-a')).toBeInTheDocument();
    expect(api.getOpenInfraHistory).toHaveBeenCalledWith(2);

    for (const name of ['Host', 'GPU', 'Engine']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument();
    }
    // Served by chat-model alone: 10 requests and 14K tokens in the last hour.
    expect(screen.getByText('14K')).toBeInTheDocument();
    // Its node and its two GPUs.
    expect(screen.getByText(/lab · 2 GPU/)).toBeInTheDocument();
    expect(screen.getAllByText('#0')).toHaveLength(1);
    expect(screen.getAllByText('#1')).toHaveLength(1);
    // Every chart, labelled for a screen reader.
    expect(screen.getByRole('img', { name: 'GPU SM (%)' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'TTFT (s)' })).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: /\)$/ }).length).toBeGreaterThanOrEqual(28);
    // Its requests, and nobody else's.
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('chat-model-instruct')).toBeInTheDocument();
    expect(screen.getByText('healthy')).toBeInTheDocument();
  });

  it('switches deployment from its tab and the address', async () => {
    render(<OpenInfraContent />);
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'code-model' }));
    expect(window.location.hash).toBe('#code-model');
    expect(screen.getByText('code-model · GPU 2,3 · node-a')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'code-model' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByText('vllm')).toBeInTheDocument();

    act(() => {
      window.history.pushState(null, '', '/openinfra#embed-model');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(screen.getByText('embed-model · GPU 0 · node-b')).toBeInTheDocument();
  });

  it('opens on the deployment and range the address names', async () => {
    window.history.replaceState(null, '', '/openinfra?hours=168#embed-model');
    render(<OpenInfraContent />);
    await loaded();

    expect(api.getOpenInfraHistory).toHaveBeenCalledWith(168);
    expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('embed-model · GPU 0 · node-b')).toBeInTheDocument();
    expect(screen.getAllByText(/^7d avg/).length).toBeGreaterThan(0);
  });

  it('loads another range when one is picked, and records it in the address', async () => {
    render(<OpenInfraContent />);
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: '24h' }));
    await waitFor(() => expect(api.getOpenInfraHistory).toHaveBeenLastCalledWith(24));
    expect(window.location.search).toBe('?hours=24');
    expect(screen.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true');
    expect((await screen.findAllByText(/^24h avg/)).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: '2h' }));
    await waitFor(() => expect(api.getOpenInfraHistory).toHaveBeenLastCalledWith(2));
    expect(window.location.search).toBe('');
  });

  it('says so when the deployment does not publish the page', async () => {
    api.getOpenInfraDashboard.mockResolvedValue({ status: 'unavailable' });
    render(<OpenInfraContent />);
    expect(await screen.findByText(/not published for this site/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Served' })).not.toBeInTheDocument();
  });

  it('reports a failed load without breaking the page', async () => {
    api.getOpenInfraDashboard.mockResolvedValue({ status: 'error' });
    render(<OpenInfraContent />);
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
  });

  it('keeps the snapshot when only the history fails', async () => {
    api.getOpenInfraHistory.mockResolvedValue({ status: 'error' });
    render(<OpenInfraContent />);
    await loaded();
    expect(screen.getByRole('status')).toHaveTextContent('The charts could not be refreshed');
    // The tiles fall back to the snapshot.
    expect(screen.getAllByText(/^live/).length).toBeGreaterThan(0);
  });

  it('keeps what it shows when a refresh fails, and says it is stale', async () => {
    render(<OpenInfraContent />);
    await loaded();
    api.getOpenInfraDashboard.mockResolvedValue({ status: 'error' });

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'The latest numbers could not be loaded',
    );
    expect(screen.getByRole('heading', { name: 'Served' })).toBeInTheDocument();
  });

  it('refreshes every 30 seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<OpenInfraContent />);
    await loaded();
    expect(api.getOpenInfraDashboard).toHaveBeenCalledTimes(1);

    expect(screen.getByText(/^Refreshed every 30 seconds/)).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(api.getOpenInfraDashboard).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000);
    });
    expect(api.getOpenInfraDashboard).toHaveBeenCalledTimes(2);
    expect(api.getOpenInfraHistory).toHaveBeenLastCalledWith(2);
  });

  it('refreshes the last 15 minutes every 5 seconds', async () => {
    window.history.replaceState(null, '', '/openinfra?hours=0.25');
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<OpenInfraContent />);
    await loaded();
    expect(api.getOpenInfraHistory).toHaveBeenLastCalledWith(0.25);
    expect(screen.getByRole('button', { name: '15m' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(/^Refreshed every 5 seconds/)).toBeInTheDocument();
    expect(screen.getAllByText(/^15m avg/).length).toBeGreaterThan(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(api.getOpenInfraDashboard).toHaveBeenCalledTimes(2);
    expect(api.getOpenInfraHistory).toHaveBeenLastCalledWith(0.25);
  });

  it('opens a chart larger, saves its CSV, and closes on Escape', async () => {
    render(<OpenInfraContent />);
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'CSV: GPU SM' }));
    expect(saved).toEqual([
      { name: expect.stringMatching(/^gpu-sm-chat-model-.+\.csv$/), mime: 'text/csv' },
    ]);

    fireEvent.click(screen.getByRole('button', { name: 'Zoom: GPU SM' }));
    const dialog = screen.getByRole('dialog', { name: 'GPU SM' });
    expect(within(dialog).getByText(/streaming multiprocessor/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Reset' })).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('leaves a plain wheel to the page, and zooms a chart with Ctrl or ⌘ held', async () => {
    render(<OpenInfraContent />);
    await loaded();
    const canvas = screen.getByRole('img', { name: 'GPU SM (%)' });
    const wheel = (init: WheelEventInit) => {
      const event = new WheelEvent('wheel', { deltaY: -100, cancelable: true, ...init });
      canvas.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(wheel({})).toBe(false);
    expect(wheel({ ctrlKey: true })).toBe(true);
    expect(wheel({ metaKey: true })).toBe(true);

    // The large view zooms on a plain wheel.
    fireEvent.click(screen.getByRole('button', { name: 'Zoom: GPU SM' }));
    const large = within(screen.getByRole('dialog')).getByRole('img', { name: 'GPU SM (%)' });
    const event = new WheelEvent('wheel', { deltaY: -100, cancelable: true });
    large.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it('offers the export to administrators only', async () => {
    render(<OpenInfraContent />);
    await loaded();
    expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument();
    cleanup();

    auth.state = { isAuthenticated: true, user: { is_admin: true } };
    api.exportOpenInfra.mockResolvedValue({
      filename: 'openinfra-export-7d.json',
      blob: new Blob(['{}']),
    });
    render(<OpenInfraContent />);
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    const dialog = screen.getByRole('dialog', { name: 'Export data' });
    fireEvent.change(within(dialog).getByLabelText('Days'), { target: { value: '7' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Download JSON' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(api.exportOpenInfra).toHaveBeenCalledWith(7);
    expect(saved).toEqual([{ name: 'openinfra-export-7d.json', mime: 'application/json' }]);
  });

  it('shows a refused export in the dialog', async () => {
    auth.state = { isAuthenticated: true, user: { is_admin: true } };
    api.exportOpenInfra.mockRejectedValue(new Error('Admin access required.'));
    render(<OpenInfraContent />);
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    fireEvent.click(screen.getByRole('button', { name: 'Download JSON' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Admin access required.');
    expect(screen.getByRole('dialog', { name: 'Export data' })).toBeInTheDocument();
    expect(api.exportOpenInfra).toHaveBeenCalledWith(30);
  });
});

describe('OpenInfraPage, the route', () => {
  afterEach(() => {
    site.openinfraUrl = '';
    redirect.mockClear();
  });

  it("serves this deployment's own page when no other is named", async () => {
    render(await OpenInfraPage());
    expect(await loaded()).toBeInTheDocument();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("redirects to another deployment's page when one is named", async () => {
    site.openinfraUrl = 'https://prod.example.test/openinfra';
    await expect(OpenInfraPage()).rejects.toThrow('NEXT_REDIRECT');
    expect(redirect).toHaveBeenCalledWith('https://prod.example.test/openinfra');
    expect(api.getOpenInfraDashboard).not.toHaveBeenCalled();
  });
});

describe('OpenInfraPage, for a reader who chose Chinese', () => {
  it('writes every section, label and description in Chinese', async () => {
    render(
      <LocaleProvider locale="zh-CN" catalog={zhCN as Catalog} moduleLocale="">
        <OpenInfraContent />
      </LocaleProvider>,
    );
    expect(await screen.findByRole('heading', { name: '服务量' })).toBeInTheDocument();
    for (const heading of ['主机', 'GPU', '推理引擎']) {
      expect(screen.getByRole('heading', { level: 2, name: heading })).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole('button', { name: '放大：TTFT' }));

    // No sentence escaped translation: runs of three or more English words
    // with no Chinese in them. Names and units are shorter than that.
    const english: string[] = [];
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const text = walk.currentNode.textContent?.trim() ?? '';
      if (/[A-Za-z]{3,}(\s+[A-Za-z]{2,}){2,}/.test(text) && !/[一-鿿]/.test(text)) {
        english.push(text);
      }
    }
    expect(english).toEqual([]);
  });
});
