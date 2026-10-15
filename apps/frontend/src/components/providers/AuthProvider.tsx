'use client';

import { useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { AccountChangedNotice } from '@/components/features/auth/AccountChangedNotice';
import {
  endAgentSession,
  login as loginApi,
  loginPhone as loginPhoneApi,
  logout as logoutApi,
} from '@/lib/api/auth';
import { ACCOUNT_CHANGED_EVENT, AUTH_EXPIRED_EVENT, setAccessToken } from '@/lib/api/client';
import { getMe } from '@/lib/api/user';
import { useSiteConfig } from './SiteConfigProvider';

/**
 * Exported because the host facade re-exports them: a distribution's UI reads
 * `useAuth().state` to choose between "Log in" and "Console", and a module
 * author should not have to redeclare the shape to type a prop.
 */
export interface AuthUser {
  id: string;
  /**
   * `null` for an account that signs in with a login name instead — the
   * first-run administrator. Display a user through `userDisplayName` /
   * `userAccountLabel` (`@/lib/utils/userLabel`) rather than this field.
   */
  email: string | null;
  login_name?: string | null;
  phone?: string | null;
  phone_verified?: boolean;
  has_password?: boolean;
  user_name?: string | null;
  role: string;
  is_admin: boolean;
}

export interface AuthState {
  isAuthenticated: boolean;
  loading: boolean;
  user: AuthUser | null;
}

interface AuthContextValue {
  state: AuthState;
  /** `identifier` is an email address or a login name; see `LoginRequest`. */
  login: (identifier: string, password: string) => Promise<void>;
  /**
   * Take over a session the backend issued outside `login()`: first-run setup
   * answers with the same body and refresh cookie as a login, so the new
   * administrator is signed in by the request that created the account.
   */
  adoptSession: (accessToken: string) => Promise<void>;
  loginPhone: (phone: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const ROLE_RANK: Record<string, number> = {
  free: 0,
  pro: 1,
  internal: 2,
  admin: 3,
};

export function hasRole(userRole: string | undefined, required: string): boolean {
  if (!(required in ROLE_RANK)) return false;
  return (ROLE_RANK[userRole ?? 'free'] ?? 0) >= ROLE_RANK[required];
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { agentsUrl } = useSiteConfig();
  const queryClient = useQueryClient();
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    loading: true,
    user: null,
  });
  const [accountChanged, setAccountChanged] = useState(false);

  const refreshUser = useCallback(async () => {
    try {
      const me = await getMe();
      setState({
        isAuthenticated: true,
        loading: false,
        user: {
          id: me.id,
          email: me.email ?? null,
          login_name: me.login_name ?? null,
          phone: me.phone,
          phone_verified: me.phone_verified,
          has_password: me.has_password,
          user_name: me.user_name,
          role: me.role || 'free',
          is_admin: me.is_admin,
        },
      });
    } catch {
      setState({ isAuthenticated: false, loading: false, user: null });
    }
  }, []);

  useEffect(() => {
    refreshUser().catch((error) => {
      console.error('Failed to refresh user:', error);
    });
  }, [refreshUser]);

  useEffect(() => {
    const handleAuthExpired = () => {
      setState({ isAuthenticated: false, loading: false, user: null });
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, []);

  useEffect(() => {
    const handleAccountChanged = () => setAccountChanged(true);
    window.addEventListener(ACCOUNT_CHANGED_EVENT, handleAccountChanged);
    return () => window.removeEventListener(ACCOUNT_CHANGED_EVENT, handleAccountChanged);
  }, []);

  // What every sign-in does once its request has installed the new token.
  // Queries are cached under keys that name no account, so the previous
  // account's answers go first; and the agent session belonged to whoever was
  // signed in before, so it ends.
  const startSession = useCallback(async () => {
    queryClient.clear();
    await endAgentSession(agentsUrl);
    await refreshUser();
  }, [agentsUrl, queryClient, refreshUser]);

  const login = useCallback(
    async (identifier: string, password: string) => {
      await loginApi({ email: identifier, password });
      await startSession();
    },
    [startSession],
  );

  // A session the backend issued outside login(), started the same way.
  const adoptSession = useCallback(
    async (accessToken: string) => {
      setAccessToken(accessToken);
      await startSession();
    },
    [startSession],
  );

  const logout = useCallback(async () => {
    try {
      await logoutApi();
    } finally {
      queryClient.clear();
      await endAgentSession(agentsUrl);
      setState({ isAuthenticated: false, loading: false, user: null });
    }
  }, [agentsUrl, queryClient]);

  const loginPhone = useCallback(
    async (phone: string, code: string) => {
      await loginPhoneApi({ phone, code });
      await startSession();
    },
    [startSession],
  );

  const value: AuthContextValue = {
    state,
    login,
    adoptSession,
    loginPhone,
    logout,
    refreshUser,
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
      {accountChanged ? <AccountChangedNotice /> : null}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
