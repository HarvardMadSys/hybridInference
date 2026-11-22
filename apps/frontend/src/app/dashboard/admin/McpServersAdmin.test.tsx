// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listAdminMcpServers = vi.fn();
const createAdminMcpServer = vi.fn();
const updateAdminMcpServer = vi.fn();
const deleteAdminMcpServer = vi.fn();

vi.mock('@/lib/api/mcpServers', () => ({
  listAdminMcpServers: (...args: unknown[]) => listAdminMcpServers(...args),
  createAdminMcpServer: (...args: unknown[]) => createAdminMcpServer(...args),
  updateAdminMcpServer: (...args: unknown[]) => updateAdminMcpServer(...args),
  deleteAdminMcpServer: (...args: unknown[]) => deleteAdminMcpServer(...args),
}));

import { McpServersAdmin } from './McpServersAdmin';

const DOCS = {
  id: 'docs',
  name: 'Docs search',
  description: 'Find pages',
  url: 'https://docs.example/mcp',
  headers: { Authorization: '${DOCS_TOKEN}' },
  docs_url: 'https://docs.example',
  icon_url: '',
};

describe('McpServersAdmin', () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    listAdminMcpServers.mockResolvedValue([DOCS]);
    createAdminMcpServer.mockResolvedValue([DOCS]);
    deleteAdminMcpServer.mockResolvedValue([]);
  });

  it('lists the store and adds a server with a secret reference', async () => {
    render(<McpServersAdmin />);

    expect(await screen.findByText('Docs search')).toBeInTheDocument();
    expect(screen.getByText(/1 header from secret settings/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }));
    const dialog = screen.getByRole('dialog', { name: 'Add MCP server' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Wiki' } });
    fireEvent.change(within(dialog).getByLabelText('URL'), {
      target: { value: 'https://wiki.example/mcp' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add server' }));

    await waitFor(() =>
      expect(createAdminMcpServer).toHaveBeenCalledWith({
        id: 'wiki',
        name: 'Wiki',
        description: '',
        url: 'https://wiki.example/mcp',
        docs_url: '',
        icon_url: '',
        headers: {},
      }),
    );
  });

  it('asks before removing a server', async () => {
    render(<McpServersAdmin />);
    await screen.findByText('Docs search');

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(screen.getByText('Remove?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(deleteAdminMcpServer).toHaveBeenCalledWith('docs'));
  });

  it('rejects a header that is not a secret reference', async () => {
    render(<McpServersAdmin />);
    await screen.findByText('Docs search');

    fireEvent.click(screen.getByRole('button', { name: '+ Add server' }));
    const dialog = screen.getByRole('dialog', { name: 'Add MCP server' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Wiki' } });
    fireEvent.change(within(dialog).getByLabelText('URL'), {
      target: { value: 'https://wiki.example/mcp' },
    });
    fireEvent.change(within(dialog).getByLabelText('Header 1 name'), {
      target: { value: 'Authorization' },
    });
    fireEvent.change(within(dialog).getByLabelText('Header 1 value'), {
      target: { value: 'Bearer plaintext' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add server' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'must reference a secret setting as ${NAME}',
    );
    expect(createAdminMcpServer).not.toHaveBeenCalled();
  });
});
