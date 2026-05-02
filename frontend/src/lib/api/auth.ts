import { fetchWithAuth, jsonOrThrow, setAccessToken, getAccessToken } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface SignupRequest {
  email: string;
  password: string;
  user_name: string;
  turnstileToken?: string;
}

export interface SignupResponse {
  message: string;
  email: string;
  user_id: string;
  requires_approval: boolean;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: {
    id: string;
    email: string;
    role: string;
    is_admin: boolean;
  };
}

export async function signup(data: SignupRequest): Promise<SignupResponse> {
  const { turnstileToken, ...rest } = data;
  const body = {
    ...rest,
    ...(turnstileToken ? { turnstile_token: turnstileToken } : {}),
  };
  const resp = await fetch(`${API_BASE}/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  });

  return jsonOrThrow<SignupResponse>(resp);
}

export async function login(data: LoginRequest): Promise<LoginResponse> {
  const resp = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    credentials: 'include',
  });

  const result = await jsonOrThrow<LoginResponse>(resp);
  setAccessToken(result.access_token);
  return result;
}

export async function logout(): Promise<void> {
  const token = getAccessToken();
  if (!token) return;

  try {
    await fetchWithAuth(API_BASE, '/auth/logout', {
      method: 'POST',
    });
  } finally {
    setAccessToken(null);
  }
}

export async function verifyEmail(token: string): Promise<{ message: string }> {
  const resp = await fetch(`${API_BASE}/auth/verify-email?token=${encodeURIComponent(token)}`, {
    method: 'GET',
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function forgotPassword(email: string): Promise<{ message: string }> {
  const resp = await fetch(`${API_BASE}/auth/forgot-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<{ message: string }> {
  const resp = await fetch(`${API_BASE}/auth/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}

export async function resendVerification(email: string): Promise<{ message: string }> {
  const resp = await fetch(`${API_BASE}/auth/resend-verification`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
    credentials: 'include',
  });

  return jsonOrThrow(resp);
}
