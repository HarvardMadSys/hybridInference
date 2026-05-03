'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { login as loginApi, logout as logoutApi } from '@/lib/api/auth';
import { getMe } from '@/lib/api/user';

interface User {
  id: string;
  email: string;
  user_name?: string | null;
  role: string;
  is_admin: boolean;
}

interface AuthState {
  isAuthenticated: boolean;
  loading: boolean;
  user: User | null;
}

interface AuthContextValue {
  state: AuthState;
  login: (email: string, password: string) => Promise<void>;
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
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    loading: true,
    user: null,
  });

  const refreshUser = useCallback(async () => {
    try {
      const me = await getMe();
      setState({
        isAuthenticated: true,
        loading: false,
        user: {
          id: me.id,
          email: me.email,
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

  const login = useCallback(
    async (email: string, password: string) => {
      await loginApi({ email, password });
      await refreshUser();
    },
    [refreshUser],
  );

  const logout = useCallback(async () => {
    await logoutApi();
    setState({ isAuthenticated: false, loading: false, user: null });
  }, []);

  const value: AuthContextValue = {
    state,
    login,
    logout,
    refreshUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
