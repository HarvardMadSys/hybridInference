// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { StrictMode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAgentTerminal,
  deleteAgentTerminal,
  listAgentTerminals,
  resizeAgentTerminal,
  streamAgentTerminal,
  writeAgentTerminalInput,
  type AgentTerminalApi,
} from '@/lib/api/agents';

import { TerminalWorkspace } from './TerminalWorkspace';

const xtermHarness = vi.hoisted(() => ({
  instances: [] as Array<{
    options: Record<string, unknown>;
    rows: number;
    cols: number;
    write: ReturnType<typeof vi.fn>;
    writeln: ReturnType<typeof vi.fn>;
    reset: ReturnType<typeof vi.fn>;
    focus: ReturnType<typeof vi.fn>;
    dispose: ReturnType<typeof vi.fn>;
    emitData: (data: string) => void;
  }>,
  fits: [] as Array<{ fit: ReturnType<typeof vi.fn> }>,
}));

vi.mock('@xterm/xterm', () => ({
  Terminal: class MockTerminal {
    options: Record<string, unknown>;
    rows = 24;
    cols = 80;
    write = vi.fn();
    writeln = vi.fn();
    reset = vi.fn();
    focus = vi.fn();
    dispose = vi.fn();
    private dataHandler: (data: string) => void = () => undefined;

    constructor(options: Record<string, unknown>) {
      this.options = options;
      xtermHarness.instances.push(this);
    }

    loadAddon() {}
    open() {}
    onData(handler: (data: string) => void) {
      this.dataHandler = handler;
      return { dispose: vi.fn() };
    }
    emitData(data: string) {
      this.dataHandler(data);
    }
  },
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class MockFitAddon {
    fit = vi.fn();
    constructor() {
      xtermHarness.fits.push(this);
    }
  },
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentTerminal: vi.fn(),
  deleteAgentTerminal: vi.fn(),
  listAgentTerminals: vi.fn(),
  resizeAgentTerminal: vi.fn(),
  streamAgentTerminal: vi.fn(),
  writeAgentTerminalInput: vi.fn(),
}));

function terminal(id: string, shell = 'zsh'): AgentTerminalApi {
  return {
    id,
    shell,
    state: 'running',
    cwd: '/workspace',
    rows: 24,
    cols: 80,
    last_seq: 0,
  };
}

function rect(left: number, right: number): DOMRect {
  return {
    bottom: 20,
    height: 20,
    left,
    right,
    top: 0,
    width: right - left,
    x: left,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

describe('TerminalWorkspace', () => {
  beforeEach(() => {
    xtermHarness.instances.length = 0;
    xtermHarness.fits.length = 0;
    vi.mocked(createAgentTerminal).mockReset();
    vi.mocked(deleteAgentTerminal).mockReset();
    vi.mocked(listAgentTerminals).mockReset();
    vi.mocked(resizeAgentTerminal).mockReset().mockResolvedValue();
    vi.mocked(writeAgentTerminalInput).mockReset().mockResolvedValue();
    vi.mocked(streamAgentTerminal)
      .mockReset()
      .mockImplementation(async (_job, _terminal, opts) => {
        opts.onOutput({ seq: 1, data: 'aGk=' });
      });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('creates, switches, splits, and kills real terminal sessions', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    const fourth = terminal('term-4');
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal)
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second)
      .mockResolvedValueOnce(third)
      .mockResolvedValueOnce(fourth);
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    render(<TerminalWorkspace jobId="job-1" active />);
    const emptyNew = await screen.findByRole('button', { name: 'New terminal' });
    fireEvent.click(emptyNew);

    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('pytest -q')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('application', { name: 'Terminal 2 terminal' })).toBeInTheDocument();
    const tablist = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    expect(within(tablist).getAllByRole('tab')).toHaveLength(2);
    expect(within(tablist).getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
      'aria-selected',
      'false',
    );
    expect(within(tablist).getByRole('tab', { name: 'Terminal 2 · bash' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    fireEvent.click(within(tablist).getByRole('tab', { name: 'Terminal 1 · zsh' }));
    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(3));
    expect(screen.getAllByRole('application')).toHaveLength(2);

    const firstPane = screen.getByRole('region', { name: 'Terminal 1 pane' });
    fireEvent.click(within(firstPane).getByRole('button', { name: 'New terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(4));
    expect(screen.getAllByRole('application')).toHaveLength(2);
    expect(screen.getByRole('application', { name: 'Terminal 4 terminal' })).toBeInTheDocument();
    expect(screen.getByRole('application', { name: 'Terminal 3 terminal' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Kill Terminal 4' }));
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-1', fourth.id));
    expect(screen.getAllByRole('application')).toHaveLength(1);
  });

  it('removes the last terminal when cleanup finishes after delete reports an error', async () => {
    const only = terminal('term-only');
    vi.mocked(listAgentTerminals).mockResolvedValueOnce([only]).mockResolvedValueOnce([]);
    vi.mocked(deleteAgentTerminal).mockRejectedValue(
      new Error('terminal cleanup could not be confirmed; retry kill'),
    );

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Kill Terminal 1' }));

    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(2));
    expect(deleteAgentTerminal).toHaveBeenCalledWith('job-1', only.id);
    expect(screen.queryByRole('application')).not.toBeInTheDocument();
    expect(screen.getByText('No open terminals')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('finishes its initial terminal load under React Strict Mode', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([]);

    render(
      <StrictMode>
        <TerminalWorkspace jobId="job-1" active />
      </StrictMode>,
    );

    expect(await screen.findByRole('button', { name: 'New terminal' })).toBeEnabled();
    expect(screen.queryByText('Loading terminals…')).not.toBeInTheDocument();
  });

  it('keeps the selected tab visible when a split narrows the first pane', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    const fourth = terminal('term-4', 'bash');
    vi.mocked(listAgentTerminals).mockResolvedValue([first, second, third]);
    vi.mocked(createAgentTerminal).mockResolvedValue(fourth);
    let split = false;
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      if (this.getAttribute('role') === 'tablist') {
        return rect(0, split ? 100 : 300);
      }
      if (this.textContent?.includes('Terminal 3')) return rect(220, 300);
      return rect(0, 80);
    });

    render(<TerminalWorkspace jobId="job-1" active />);

    fireEvent.click(await screen.findByRole('tab', { name: 'Terminal 3 · zsh' }));

    expect(
      await screen.findByRole('application', { name: 'Terminal 3 terminal' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: 'Terminals in pane 1' }).scrollLeft).toBe(0);

    split = true;
    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));

    const firstPane = await screen.findByRole('region', { name: 'Terminal 3 pane' });
    const tablist = within(firstPane).getByRole('tablist', { name: 'Terminals in pane 1' });
    await waitFor(() => expect(tablist.scrollLeft).toBe(200));
  });

  it('keeps the selected tab visible when the tab strip resizes', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    vi.mocked(listAgentTerminals).mockResolvedValue([first, second, third]);
    let paneWidth = 300;
    let resizeTabList: (() => void) | undefined;

    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(target: Element) {
          if (target.getAttribute('role') === 'tablist') {
            resizeTabList = () => this.callback([], this as unknown as ResizeObserver);
          }
        }
        disconnect() {}
        unobserve() {}
      },
    );
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      if (this.getAttribute('role') === 'tablist') return rect(0, paneWidth);
      if (this.textContent?.includes('Terminal 3')) return rect(220, 300);
      return rect(0, 80);
    });

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Terminal 3 · zsh' }));

    const tablist = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    expect(tablist.scrollLeft).toBe(0);
    await waitFor(() => expect(resizeTabList).toBeTypeOf('function'));

    paneWidth = 100;
    resizeTabList?.();

    expect(tablist.scrollLeft).toBe(200);
  });

  it('swaps panes when selecting a terminal already visible in the other pane', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    vi.mocked(createAgentTerminal).mockResolvedValue(second);

    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });

    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));
    await screen.findByRole('application', { name: 'Terminal 2 terminal' });

    const firstPane = screen.getByRole('region', { name: 'Terminal 1 pane' });
    fireEvent.click(within(firstPane).getByRole('tab', { name: 'Terminal 2 · bash' }));

    await waitFor(() => {
      const firstPaneTabs = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
      const secondPaneTabs = screen.getByRole('tablist', { name: 'Terminals in pane 2' });
      expect(within(firstPaneTabs).getByRole('tab', { name: 'Terminal 2 · bash' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
      expect(within(secondPaneTabs).getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
    expect(screen.getAllByRole('application', { name: 'Terminal 1 terminal' })).toHaveLength(1);
    expect(screen.getAllByRole('application', { name: 'Terminal 2 terminal' })).toHaveLength(1);
  });

  it('writes streamed bytes, sends xterm input, fits and resizes, then cleans up', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    const streamSignal = { current: undefined as AbortSignal | undefined };
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      streamSignal.current = opts.signal;
      opts.onReset({ seq: 1, reason: 'output_truncated' });
      opts.onOutput({ seq: 1, data: 'aGk=' });
    });

    const view = render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    const xterm = xtermHarness.instances[0];

    expect(xterm.reset).toHaveBeenCalled();
    expect(xterm.writeln).toHaveBeenCalledWith('\x1b[90m[Earlier output truncated]\x1b[0m');
    expect(Array.from(xterm.write.mock.calls[0][0] as Uint8Array)).toEqual([104, 105]);
    expect(xtermHarness.fits[0].fit).toHaveBeenCalled();
    expect(resizeAgentTerminal).toHaveBeenCalledWith('job-1', first.id, 24, 80);

    xterm.emitData('ls\r');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'ls\r'),
    );

    view.unmount();
    expect(streamSignal.current?.aborted).toBe(true);
    expect(xterm.dispose).toHaveBeenCalled();
  });

  it('keeps running jobs read-only and explains why', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);

    const view = render(
      <TerminalWorkspace
        jobId="job-1"
        active
        disabled
        disabledReason="Wait for the agent to finish."
      />,
    );

    expect(listAgentTerminals).not.toHaveBeenCalled();
    expect(screen.getByRole('note')).toHaveTextContent('Wait for the agent to finish.');
    expect(screen.queryByRole('application')).not.toBeInTheDocument();

    view.rerender(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('region', { name: 'Terminal 1 pane' });
    expect(listAgentTerminals).toHaveBeenCalledWith('job-1');
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    expect(xtermHarness.instances[0].options.disableStdin).toBe(false);
    xtermHarness.instances[0].emitData('pwd\r');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'pwd\r'),
    );
  });

  it('cleans up a terminal created after navigating to another job', async () => {
    const stale = terminal('term-stale');
    let resolveCreate!: (session: AgentTerminalApi) => void;
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockImplementation(
      () =>
        new Promise<AgentTerminalApi>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));
    view.rerender(<TerminalWorkspace jobId="job-b" active />);
    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledWith('job-b'));

    resolveCreate(stale);
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', stale.id));
    expect(screen.queryByRole('application')).not.toBeInTheDocument();
  });

  it('cleans up a terminal whose creation finishes after unmount', async () => {
    const stale = terminal('term-stale');
    let resolveCreate!: (session: AgentTerminalApi) => void;
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockImplementation(
      () =>
        new Promise<AgentTerminalApi>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));
    view.unmount();
    resolveCreate(stale);

    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', stale.id));
  });

  it('shows a load error and retries before allowing new sessions', async () => {
    const existing = terminal('term-existing');
    vi.mocked(listAgentTerminals)
      .mockRejectedValueOnce(new Error('Workspace is unavailable'))
      .mockResolvedValueOnce([existing]);

    render(<TerminalWorkspace jobId="job-1" active />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Workspace is unavailable');
    expect(screen.getByText('No open terminals')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New terminal' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(listAgentTerminals).toHaveBeenCalledTimes(2);
  });

  it('does not update UI when a kill finishes after the job changes', async () => {
    const oldTerminal = terminal('term-old');
    const newTerminal = terminal('term-new');
    let resolveKill!: () => void;
    vi.mocked(listAgentTerminals).mockImplementation(async (jobId) =>
      jobId === 'job-a' ? [oldTerminal] : [newTerminal],
    );
    vi.mocked(deleteAgentTerminal).mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveKill = resolve;
        }),
    );

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Kill Terminal 1' }));
    expect(screen.getByRole('tab', { name: 'Terminal 1 · zsh' })).toBeDisabled();
    view.rerender(<TerminalWorkspace jobId="job-b" active />);
    const selectedTab = await screen.findByRole('tab', { name: 'Terminal 1 · zsh' });
    expect(selectedTab).toHaveAttribute('aria-selected', 'true');
    expect(selectedTab).toBeEnabled();

    resolveKill();
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', oldTerminal.id));
    expect(selectedTab).toHaveAttribute('aria-selected', 'true');
  });

  it('batches input while preserving byte order across an in-flight write', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let releaseFirst!: () => void;
    vi.mocked(writeAgentTerminalInput)
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            releaseFirst = resolve;
          }),
      )
      .mockResolvedValueOnce();

    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));

    xtermHarness.instances[0].emitData('a');
    xtermHarness.instances[0].emitData('b');
    await waitFor(() => expect(writeAgentTerminalInput).toHaveBeenCalledTimes(1));
    expect(writeAgentTerminalInput).toHaveBeenNthCalledWith(1, 'job-1', first.id, 'ab');

    xtermHarness.instances[0].emitData('c');
    xtermHarness.instances[0].emitData('d');
    releaseFirst();
    await waitFor(() => expect(writeAgentTerminalInput).toHaveBeenCalledTimes(2));
    expect(writeAgentTerminalInput).toHaveBeenNthCalledWith(2, 'job-1', first.id, 'cd');
  });
});
