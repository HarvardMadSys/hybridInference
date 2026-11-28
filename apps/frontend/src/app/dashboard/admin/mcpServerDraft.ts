import type { AdminMcpServer, AdminMcpServerInput } from '@/lib/api/mcpServers';

export const MCP_FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50';

const REFERENCE = /^\$\{MCP_[A-Za-z0-9_]+\}$/;
const ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export interface HeaderRow {
  name: string;
  value: string;
}

export interface Draft {
  id: string;
  name: string;
  description: string;
  url: string;
  docsUrl: string;
  iconUrl: string;
  headers: HeaderRow[];
}

export function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

export function toDraft(server?: AdminMcpServer): Draft {
  return {
    id: server?.id ?? '',
    name: server?.name ?? '',
    description: server?.description ?? '',
    url: server?.url ?? '',
    docsUrl: server?.docs_url ?? '',
    iconUrl: server?.icon_url ?? '',
    headers: server
      ? Object.entries(server.headers).map(([name, value]) => ({ name, value }))
      : [{ name: '', value: '' }],
  };
}

export function toInput(draft: Draft): AdminMcpServerInput {
  const headers: Record<string, string> = {};
  for (const row of draft.headers) {
    if (row.name.trim() && row.value.trim()) headers[row.name.trim()] = row.value.trim();
  }
  return {
    id: draft.id.trim(),
    name: draft.name.trim(),
    description: draft.description.trim(),
    url: draft.url.trim(),
    docs_url: draft.docsUrl.trim(),
    icon_url: draft.iconUrl.trim(),
    headers,
  };
}

export function validate(draft: Draft): string | null {
  if (!draft.name.trim()) return 'A name is required.';
  if (!draft.id.trim()) return 'An id is required.';
  if (!ID.test(draft.id.trim())) {
    return 'Use lowercase letters, digits, _ or -, starting with a letter or digit.';
  }
  if (!draft.url.trim()) return 'A URL is required.';
  for (const row of draft.headers) {
    if (!row.name.trim() && !row.value.trim()) continue;
    if (!row.name.trim() || !row.value.trim()) return 'Every header needs a name and a value.';
    if (!REFERENCE.test(row.value.trim())) {
      return `Header ${row.name.trim()} must reference an MCP_ secret setting as \${MCP_NAME}.`;
    }
  }
  return null;
}
