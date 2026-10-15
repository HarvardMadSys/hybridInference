'use client';

import { useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AccountChangedNotice } from '@/components/features/auth/AccountChangedNotice';
import {
  agentSessionUser,
  endAgentSession,
  listAccounts,
  login as loginApi,
  loginPhone as loginPhoneApi,
  logout as logoutApi,
  logoutAll as logoutAllApi,
  removeAccount as removeAccountApi,
  switchAccount as switchAccountApi,
  type AccountSummary,
} from '@/lib/api/auth';
import {
  ACCOUNT_CHANGED_EVENT,
  AUTH_EXPIRED_EVENT,
  markAccountChanged,
  setAccessToken,
  type AccountChangeReason,
} from '@/lib/api/client';
import { announceAccounts, listenForAccounts } from '@/lib/auth/accountSync';
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
  /** Sign the current account out of this browser; its other accounts stay. */
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  /** The accounts signed in on this browser, for the account chooser and menu. */
  accounts: AccountSummary[];
  /** How many accounts one browser may hold. */
  maxAccounts: number;
  /** False until the first account listing has answered, or failed. */
  accountsReady: boolean;
  refreshAccounts: () => Promise<void>;
  /** Make another signed-in account this browser's, without a password. */
  switchAccount: (userId: string) => Promise<void>;
  /** Sign one account out of this browser, the current one included. */
  removeAccount: (userId: string) => Promise<void>;
  logoutAll: () => Promise<void>;
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
  const [accountChanged, setAccountChanged] = useState<AccountChangeReason | null>(null);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [maxAccounts, setMaxAccounts] = useState(5);
  const [accountsReady, setAccountsReady] = useState(false);
  // Read by the cross-tab listener, which is installed once.
  const userIdRef = useRef<string | null>(null);
  const accountsRef = useRef<AccountSummary[]>([]);
  userIdRef.current = state.user?.id ?? null;
  accountsRef.current = accounts;

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

  const refreshAccounts = useCallback(async () => {
    try {
      const listing = await listAccounts();
      setAccounts(listing.accounts);
      setMaxAccounts(listing.max_accounts);
    } catch {
      // The chooser is a convenience; signing in with a password still works.
    } finally {
      setAccountsReady(true);
    }
  }, []);

  useEffect(() => {
    // In turn, not at once: both can replace a cookie issued before browser
    // sessions existed, and each would replace it differently.
    refreshUser()
      .then(() => refreshAccounts())
      .catch((error) => {
        console.error('Failed to refresh user:', error);
      });
  }, [refreshUser, refreshAccounts]);

  useEffect(() => {
    const handleAuthExpired = () => {
      setState({ isAuthenticated: false, loading: false, user: null });
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, []);

  useEffect(() => {
    const handleAccountChanged = (event: Event) => {
      const reason = (event as CustomEvent<AccountChangeReason>).detail;
      setAccountChanged(reason === 'signed-out' ? 'signed-out' : 'switched');
    };
    window.addEventListener(ACCOUNT_CHANGED_EVENT, handleAccountChanged);
    return () => window.removeEventListener(ACCOUNT_CHANGED_EVENT, handleAccountChanged);
  }, []);

  useEffect(
    () =>
      listenForAccounts((message) => {
        const shown = userIdRef.current;
        if (message.type === 'active' && shown && message.userId !== shown) {
          // The page under this tab belongs to an account the browser is no
          // longer using: stop here rather than at the next token refresh.
          markAccountChanged(message.userId ? 'switched' : 'signed-out');
          return;
        }
        if (message.type === 'active' && !shown && message.userId) {
          // Signed in elsewhere while this tab had no account: take it up.
          void refreshUser();
        }
        void refreshAccounts();
      }),
    [refreshAccounts, refreshUser],
  );

  /**
   * End the Agent's session when it belongs to one of `userIds`, or to anyone
   * when that cannot be told. Signing an account out of the console must not
   * leave the Agent signed in as it; signing another account out must not end
   * an Agent session that is still wanted.
   */
  const endAgentSessionOf = useCallback(
    async (userIds: string[]) => {
      if (!agentsUrl) return;
      const agentUser = await agentSessionUser(agentsUrl);
      if (agentUser === null) return;
      if (agentUser !== undefined && !userIds.includes(agentUser)) return;
      await endAgentSession(agentsUrl);
    },
    [agentsUrl],
  );

  // What every sign-in does once its request has installed the new token.
  // Queries are cached under keys that name no account, so the previous
  // account's answers go first. The Agent session stays when the account joins
  // a browser that has others signed in — it is one of theirs — and ends on a
  // browser that had none, where it can only belong to someone who left.
  const startSession = useCallback(
    async (userId: string, hadAccounts: boolean) => {
      queryClient.clear();
      if (!hadAccounts) await endAgentSession(agentsUrl);
      await refreshUser();
      await refreshAccounts();
      announceAccounts({ type: 'active', userId });
    },
    [agentsUrl, queryClient, refreshAccounts, refreshUser],
  );

  const signedInHere = () => accountsRef.current.some((account) => account.signed_in);

  const login = useCallback(
    async (identifier: string, password: string) => {
      const hadAccounts = signedInHere();
      const result = await loginApi({ email: identifier, password });
      await startSession(result.user.id, hadAccounts);
    },
    [startSession],
  );

  // A session the backend issued outside login(), started the same way.
  const adoptSession = useCallback(
    async (accessToken: string) => {
      const hadAccounts = signedInHere();
      setAccessToken(accessToken);
      queryClient.clear();
      if (!hadAccounts) await endAgentSession(agentsUrl);
      await refreshUser();
      await refreshAccounts();
      announceAccounts({ type: 'active', userId: userIdRef.current });
    },
    [agentsUrl, queryClient, refreshAccounts, refreshUser],
  );

  const loginPhone = useCallback(
    async (phone: string, code: string) => {
      const hadAccounts = signedInHere();
      const result = await loginPhoneApi({ phone, code });
      await startSession(result.user.id, hadAccounts);
    },
    [startSession],
  );

  const switchAccount = useCallback(
    async (userId: string) => {
      await switchAccountApi(userId);
      queryClient.clear();
      await refreshUser();
      await refreshAccounts();
      announceAccounts({ type: 'active', userId });
    },
    [queryClient, refreshAccounts, refreshUser],
  );

  const signedOut = useCallback(
    async (userIds: string[] | null) => {
      queryClient.clear();
      if (userIds) await endAgentSessionOf(userIds);
      else await endAgentSession(agentsUrl);
      setState({ isAuthenticated: false, loading: false, user: null });
      await refreshAccounts();
      announceAccounts({ type: 'active', userId: null });
    },
    [agentsUrl, endAgentSessionOf, queryClient, refreshAccounts],
  );

  const logout = useCallback(async () => {
    const userId = userIdRef.current;
    try {
      await logoutApi();
    } finally {
      await signedOut(userId ? [userId] : null);
    }
  }, [signedOut]);

  const logoutAll = useCallback(async () => {
    try {
      await logoutAllApi();
    } finally {
      await signedOut(null);
    }
  }, [signedOut]);

  const removeAccount = useCallback(
    async (userId: string) => {
      if (userId === userIdRef.current) {
        await logout();
        return;
      }
      const listing = await removeAccountApi(userId);
      setAccounts(listing.accounts);
      await endAgentSessionOf([userId]);
      announceAccounts({ type: 'accounts' });
    },
    [endAgentSessionOf, logout],
  );

  const value: AuthContextValue = {
    state,
    login,
    adoptSession,
    loginPhone,
    logout,
    refreshUser,
    accounts,
    maxAccounts,
    accountsReady,
    refreshAccounts,
    switchAccount,
    removeAccount,
    logoutAll,
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
      {accountChanged ? <AccountChangedNotice reason={accountChanged} /> : null}
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
