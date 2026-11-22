/** Admin catalog management for the Playground's MCP server store. */

import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const API_BASE = config.apiBase;

export interface AdminMcpServer {
  id: string;
  name: string;
  description: string;
  url: string;
  headers: Record<string, string>;
  docs_url: string;
  icon_url: string;
}

export interface AdminMcpServerInput {
  id: string;
  name: string;
  description: string;
  url: string;
  headers: Record<string, string>;
  docs_url: string;
  icon_url: string;
}

async function readServers(resp: Response): Promise<AdminMcpServer[]> {
  const data = await jsonOrThrow<{ servers: AdminMcpServer[] }>(resp);
  return Array.isArray(data.servers) ? data.servers : [];
}

export async function listAdminMcpServers(): Promise<AdminMcpServer[]> {
  const resp = await fetchWithAuth(API_BASE, '/admin/mcp-servers');
  return readServers(resp);
}

export async function createAdminMcpServer(input: AdminMcpServerInput): Promise<AdminMcpServer[]> {
  const resp = await fetchWithAuth(API_BASE, '/admin/mcp-servers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return readServers(resp);
}

export async function updateAdminMcpServer(
  id: string,
  input: AdminMcpServerInput,
): Promise<AdminMcpServer[]> {
  const resp = await fetchWithAuth(API_BASE, `/admin/mcp-servers/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return readServers(resp);
}

export async function deleteAdminMcpServer(id: string): Promise<AdminMcpServer[]> {
  const resp = await fetchWithAuth(API_BASE, `/admin/mcp-servers/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  return readServers(resp);
}
