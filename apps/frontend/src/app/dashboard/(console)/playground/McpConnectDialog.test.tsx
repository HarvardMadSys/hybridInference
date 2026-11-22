// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chooseOption } from '@/test-utils/select';

const fetchWithAuth = vi.fn();

vi.mock('@/lib/api/client', () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuth(...args),
  jsonOrThrow: async (resp: { json: () => Promise<unknown> }) => resp.json(),
}));

import { McpConnectDialog, mcpAuthHeaders } from './McpConnectDialog';

describe('mcpAuthHeaders', () => {
  it('sends nothing without authentication', () => {
    expect(mcpAuthHeaders('none', 'secret', [{ name: 'X-Key', value: 'v' }])).toEqual({});
  });

  it('turns a bare token into a Bearer Authorization header', () => {
    expect(mcpAuthHeaders('token', '  abc123 ', [])).toEqual({ Authorization: 'Bearer abc123' });
  });

  it('keeps a token that already names the Bearer scheme', () => {
    expect(mcpAuthHeaders('token', 'bearer abc', [])).toEqual({ Authorization: 'bearer abc' });
  });

  it('sends no Authorization header for an empty token', () => {
    expect(mcpAuthHeaders('token', '   ', [])).toEqual({});
  });

  it('keeps only complete custom header rows', () => {
    expect(
      mcpAuthHeaders('headers', '', [
        { name: ' X-Api-Key ', value: ' k ' },
        { name: '', value: 'orphan' },
        { name: 'X-Empty', value: '' },
      ]),
    ).toEqual({ 'X-Api-Key': 'k' });
  });
});

describe('McpConnectDialog', () => {
  afterEach(() => {
    cleanup();
    fetchWithAuth.mockReset();
  });

  beforeEach(() => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({ servers: [] }),
    });
  });

  it('connects with the URL, description and a Bearer token', async () => {
    const onConnect = vi.fn().mockResolvedValue(undefined);
    render(<McpConnectDialog onClose={vi.fn()} onConnect={onConnect} />);

    expect(screen.getByRole('dialog', { name: 'Add MCP server' })).toBeInTheDocument();
    expect(screen.getByText('Only use MCP servers you trust and verify.')).toBeInTheDocument();
    const connect = screen.getByRole('button', { name: 'Connect' });
    expect(connect).toBeDisabled();

    fireEvent.change(screen.getByLabelText('MCP server URL'), {
      target: { value: ' https://mcp.example/mcp ' },
    });
    fireEvent.change(screen.getByLabelText(/Description/), { target: { value: 'Weather tools' } });
    chooseOption('Authentication', 'Access token / API key');
    fireEvent.change(screen.getByLabelText('access token'), { target: { value: 'tok' } });
    fireEvent.click(connect);

    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith({
        url: 'https://mcp.example/mcp',
        description: 'Weather tools',
        headers: { Authorization: 'Bearer tok' },
      }),
    );
  });

  it('masks the token until shown', () => {
    render(<McpConnectDialog onClose={vi.fn()} onConnect={vi.fn()} />);
    chooseOption('Authentication', 'Access token / API key');
    const token = screen.getByLabelText('access token');
    expect(token).toHaveAttribute('type', 'password');
    fireEvent.click(screen.getByRole('button', { name: 'Show access token' }));
    expect(token).toHaveAttribute('type', 'text');
    fireEvent.click(screen.getByRole('button', { name: 'Hide access token' }));
    expect(token).toHaveAttribute('type', 'password');
  });

  it('sends custom headers and masks their values', async () => {
    const onConnect = vi.fn().mockResolvedValue(undefined);
    render(<McpConnectDialog onClose={vi.fn()} onConnect={onConnect} />);
    fireEvent.change(screen.getByLabelText('MCP server URL'), {
      target: { value: 'https://mcp.example/mcp' },
    });
    chooseOption('Authentication', 'Custom headers');
    fireEvent.change(screen.getByLabelText('Header 1 name'), { target: { value: 'X-Api-Key' } });
    const value = screen.getByLabelText('header 1 value');
    expect(value).toHaveAttribute('type', 'password');
    fireEvent.change(value, { target: { value: 'k1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith(
        expect.objectContaining({ headers: { 'X-Api-Key': 'k1' } }),
      ),
    );
  });

  it('stays open with the error when connecting fails', async () => {
    const onClose = vi.fn();
    const onConnect = vi.fn().mockRejectedValue(new Error('The MCP server request failed.'));
    render(<McpConnectDialog onClose={onClose} onConnect={onConnect} />);
    fireEvent.change(screen.getByLabelText('MCP server URL'), {
      target: { value: 'https://mcp.example/mcp' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The MCP server request failed.');
    expect(screen.getByRole('button', { name: 'Connect' })).not.toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('connects a store server by id and keeps its credentials server-side', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({
        servers: [
          {
            id: 'docs',
            name: 'Docs search',
            description: 'Find pages',
            url: 'https://docs.example/mcp',
            docsUrl: '',
          },
        ],
      }),
    });
    const onConnect = vi.fn().mockResolvedValue(undefined);
    render(<McpConnectDialog onClose={vi.fn()} onConnect={onConnect} />);

    fireEvent.click(await screen.findByRole('button', { name: /Docs search/ }));

    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith({
        url: 'https://docs.example/mcp',
        description: 'Find pages',
        headers: {},
        catalogId: 'docs',
        name: 'Docs search',
      }),
    );
    const call = fetchWithAuth.mock.calls.find(
      (args) => args[1] === '/internal/playground/mcp/servers',
    );
    expect(call).toBeDefined();
  });

  it('closes on Escape and on Cancel', () => {
    const onClose = vi.fn();
    render(<McpConnectDialog onClose={onClose} onConnect={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
