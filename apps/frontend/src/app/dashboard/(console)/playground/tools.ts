/** MCP tool discovery and the per-format shapes the playground sends. */

import type { HistoryMessage, ToolCall } from './turns';

export interface McpTool {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
}

export interface McpServer {
  id: string;
  url: string;
  headers: Record<string, string>;
  tools: McpTool[];
}

export interface SelectedTool {
  serverId: string;
  serverUrl: string;
  serverHeaders: Record<string, string>;
  tool: McpTool;
}

export interface ServerSelection {
  serverId: string;
  name: string;
}

/** OpenAI and Anthropic both constrain tool names; the wire name is a safe alias. */
export function wireName(name: string): string {
  return name.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64) || 'tool';
}

export function selectedTools(servers: McpServer[], selection: ServerSelection[]): SelectedTool[] {
  const tools: SelectedTool[] = [];
  for (const pick of selection) {
    const server = servers.find((candidate) => candidate.id === pick.serverId);
    const tool = server?.tools.find((candidate) => candidate.name === pick.name);
    if (server && tool) {
      tools.push({
        serverId: server.id,
        serverUrl: server.url,
        serverHeaders: server.headers,
        tool,
      });
    }
  }
  return tools;
}

export function findTool(tools: SelectedTool[], name: string): SelectedTool | undefined {
  return tools.find((tool) => wireName(tool.tool.name) === wireName(name));
}

/** Attach the server and id the model's tool call refers to, once it names one. */
export function resolveToolCall(call: ToolCall, tools: SelectedTool[]): ToolCall {
  if (call.serverUrl) return call;
  const tool = findTool(tools, call.name);
  if (!tool) return call;
  return {
    ...call,
    mcpName: tool.tool.name,
    serverUrl: tool.serverUrl,
    serverHeaders: tool.serverHeaders,
  };
}

export function chatTools(tools: SelectedTool[]): Record<string, unknown>[] {
  return tools.map((tool) => ({
    type: 'function',
    function: {
      name: wireName(tool.tool.name),
      ...(tool.tool.description ? { description: tool.tool.description } : {}),
      parameters: tool.tool.inputSchema,
    },
  }));
}

export function responsesTools(tools: SelectedTool[]): Record<string, unknown>[] {
  return tools.map((tool) => ({
    type: 'function',
    name: wireName(tool.tool.name),
    ...(tool.tool.description ? { description: tool.tool.description } : {}),
    parameters: tool.tool.inputSchema,
  }));
}

export function messagesTools(tools: SelectedTool[]): Record<string, unknown>[] {
  return tools.map((tool) => ({
    name: wireName(tool.tool.name),
    ...(tool.tool.description ? { description: tool.tool.description } : {}),
    input_schema: tool.tool.inputSchema,
  }));
}

function parseArguments(raw: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(raw || '{}');
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function completedToolCalls(message: HistoryMessage): ToolCall[] {
  return (message.toolCalls ?? []).filter(
    (call) => call.status === 'done' && typeof call.result === 'string',
  );
}

/** The history in the body shape the selected API format expects. */
export function formatHistory(
  api: 'chat' | 'responses' | 'messages',
  history: HistoryMessage[],
): Record<string, unknown>[] {
  if (api === 'chat') return chatHistory(history);
  if (api === 'responses') return responsesHistory(history);
  return messagesHistory(history);
}

function chatHistory(history: HistoryMessage[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const message of history) {
    const calls = completedToolCalls(message);
    if (message.role === 'assistant' && calls.length > 0) {
      out.push({
        role: 'assistant',
        content: message.content,
        tool_calls: calls.map((call) => ({
          id: call.id,
          type: 'function',
          function: { name: wireName(call.name), arguments: call.arguments },
        })),
      });
      for (const call of calls) {
        out.push({ role: 'tool', tool_call_id: call.id, content: call.result });
      }
      continue;
    }
    out.push({ role: message.role, content: message.content });
  }
  return out;
}

function responsesHistory(history: HistoryMessage[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const message of history) {
    const calls = completedToolCalls(message);
    if (message.role === 'assistant' && calls.length > 0) {
      if (message.content) out.push({ role: 'assistant', content: message.content });
      for (const call of calls) {
        out.push({
          type: 'function_call',
          call_id: call.id,
          name: wireName(call.name),
          arguments: call.arguments,
        });
      }
      for (const call of calls) {
        out.push({ type: 'function_call_output', call_id: call.id, output: call.result });
      }
      continue;
    }
    out.push({ role: message.role, content: message.content });
  }
  return out;
}

function messagesHistory(history: HistoryMessage[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const message of history) {
    const calls = completedToolCalls(message);
    if (message.role === 'assistant' && calls.length > 0) {
      out.push({
        role: 'assistant',
        content: [
          ...(message.content ? [{ type: 'text', text: message.content }] : []),
          ...calls.map((call) => ({
            type: 'tool_use',
            id: call.id,
            name: wireName(call.name),
            input: parseArguments(call.arguments),
          })),
        ],
      });
      out.push({
        role: 'user',
        content: calls.map((call) => ({
          type: 'tool_result',
          tool_use_id: call.id,
          content: call.result,
        })),
      });
      continue;
    }
    out.push({ role: message.role, content: message.content });
  }
  return out;
}
