'use client';

import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { login as loginApi, logout as logoutApi } from '@/lib/api/auth';
import { getMe } from '@/lib/api/user';

interface User {
  id: string;
  email: string;
  tier: string;
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
        user: { id: me.id, email: me.email, tier: me.tier },
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
