import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface User {
  id: string;
  email: string;
  user_name?: string;
  tier: string;
  status: string;
  email_verified: boolean;
  created_at: string;
}

export interface ApiKeyResponse {
  api_key: string;
  key_prefix: string;
  warning: string;
  created_at: string;
}

export interface ApiKeyInfo {
  has_key: boolean;
  key_prefix?: string;
  key_masked?: string;
  created_at?: string;
  last_used_at?: string;
  status?: string;
}

export interface UsageStats {
  period: 'today' | 'month' | 'all';
  quota: {
    has_key: boolean;
    daily_limit_usd?: number;
    monthly_limit_usd?: number;
    spent_today_usd?: number;
    spent_month_usd?: number;
    remaining_today_usd?: number;
  };
  usage: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost_usd: number;
  };
}

export async function getMe(): Promise<User> {
  const resp = await fetchWithAuth(API_BASE, '/user/me');
  return jsonOrThrow<User>(resp);
}

export async function createApiKey(): Promise<ApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys', {
    method: 'POST',
  });
  return jsonOrThrow<ApiKeyResponse>(resp);
}

export async function getApiKey(): Promise<ApiKeyInfo> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys');
  return jsonOrThrow<ApiKeyInfo>(resp);
}

export async function regenerateApiKey(): Promise<ApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys/regenerate', {
    method: 'POST',
  });
  return jsonOrThrow<ApiKeyResponse>(resp);
}

export async function getUsage(period: 'today' | 'month' | 'all' = 'today'): Promise<UsageStats> {
  const resp = await fetchWithAuth(API_BASE, `/user/usage?period=${period}`);
  return jsonOrThrow<UsageStats>(resp);
}

export async function updateProfile(data: { user_name?: string }): Promise<User> {
  const resp = await fetchWithAuth(API_BASE, '/user/profile', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow<User>(resp);
}

export async function updatePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ message: string }> {
  const resp = await fetchWithAuth(API_BASE, '/user/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: currentPassword, new_password: newPassword }),
  });
  return jsonOrThrow<{ message: string }>(resp);
}

export async function changeEmail(
  newEmail: string,
  password: string,
): Promise<{ message: string; new_email: string }> {
  const resp = await fetchWithAuth(API_BASE, '/user/change-email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_email: newEmail, password }),
  });
  return jsonOrThrow<{ message: string; new_email: string }>(resp);
}
