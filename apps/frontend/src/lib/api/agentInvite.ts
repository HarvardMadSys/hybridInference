import { jsonOrThrow, safeFetch, setAccessToken } from './client';
import { config } from '@/config/env';
import type { LoginResponse } from './auth';

const API_BASE = config.apiBase;

export interface AgentInviteRedeemResponse extends LoginResponse {
  api_key: string;
  account_created: boolean;
}

/**
 * Redeem an agent invitation: set this account's password and sign in.
 *
 * The invitation token names the account (the gateway binds it to the
 * invited email), so the page sends no email address. The response is the
 * login response plus the agent-scoped API key, shown once: it is the key
 * the user pastes into the agent service as a model connection.
 */
export async function redeemAgentInvite(data: {
  token: string;
  password: string;
  userName?: string;
  acceptedTos: boolean;
}): Promise<AgentInviteRedeemResponse> {
  const resp = await safeFetch(`${API_BASE}/v1/agent-invites/redeem`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      token: data.token,
      password: data.password,
      ...(data.userName ? { user_name: data.userName } : {}),
      accepted_tos: data.acceptedTos,
    }),
    credentials: 'include',
  });
  const result = await jsonOrThrow<AgentInviteRedeemResponse>(resp);
  setAccessToken(result.access_token);
  return result;
}
