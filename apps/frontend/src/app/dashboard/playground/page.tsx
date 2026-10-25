'use client';

import Link from 'next/link';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ChangeEvent,
} from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { hasRole } from '@/components/providers/AuthProvider';
import { Markdown } from '@/components/ui/Markdown';
import { Select } from '@/components/ui/Select';
import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';
import { ModelPicker, type PlaygroundModel } from './ModelPicker';
import {
  decodeRate,
  finishNote,
  isUsableReply,
  requestHistory,
  type Message,
  type RoutingInfo,
} from './turns';

const API_BASE = config.apiBase;

/** How close to the bottom (px) still counts as following the stream. */
const FOLLOW_THRESHOLD = 48;
const UNDO_MS = 8000;
/**
 * The backend sends at least a keepalive every 15 s, so this much silence means
 * the connection is gone -- even when a proxy in between keeps it open, as the
 * console's own rewrite proxy does after the backend drops.
 */
const STREAM_IDLE_MS = 45_000;
const NO_MESSAGES: Message[] = [];

interface PlaygroundSession {
  id: string;
  title: string;
  selectedModelId: string;
  selectedProvider: string | null;
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  messages: Message[];
  input: string;
}

interface UndoAction {
  label: string;
  restore: () => void;
  /** The session whose action this is: the timer pauses only while *it* streams. */
  sessionId: string;
}

let messageCounter = 0;

function messageId(): string {
  messageCounter += 1;
  return `m-${messageCounter}`;
}

function createSession(id: string, defaultModelId = ''): PlaygroundSession {
  return {
    id,
    title: 'New session',
    selectedModelId: defaultModelId,
    selectedProvider: null,
    systemPrompt: '',
    temperature: 0.7,
    maxTokens: 4096,
    messages: [],
    input: '',
  };
}

function getSessionTitle(session: PlaygroundSession): string {
  const first = session.messages.find((m) => m.role === 'user')?.content.trim();
  if (first) {
    return first.length > 40 ? `${first.slice(0, 40)}...` : first;
  }
  return session.title;
}

function getMessageCount(messages: Message[]): string {
  if (messages.length === 0) return 'No messages';
  if (messages.length === 1) return '1 message';
  return `${messages.length} messages`;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** An in-band `error` frame's message, whatever shape the upstream used. */
function frameError(raw: unknown): string | null {
  if (!raw) return null;
  if (typeof raw === 'string') return raw;
  if (typeof raw === 'object') {
    const message = (raw as Record<string, unknown>).message;
    if (typeof message === 'string' && message) return message;
  }
  return 'The upstream reported an error.';
}

/** Read a `_playground_route` frame; returns null for anything unrecognized. */
function parseRouting(raw: unknown): RoutingInfo | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const endpointId = typeof r.endpoint_id === 'string' ? r.endpoint_id : '';
  if (!endpointId) return null;
  const rawAttempts = Array.isArray(r.failed_attempts) ? r.failed_attempts : [];
  const attempts = rawAttempts
    .filter((a): a is Record<string, unknown> => !!a && typeof a === 'object')
    .map((a) => ({
      provider: String(a.provider ?? ''),
      endpointId: String(a.endpoint_id ?? ''),
      errorType: String(a.error_type ?? ''),
    }));
  return {
    provider: typeof r.provider === 'string' ? r.provider : '',
    endpointId,
    ...(typeof r.host === 'string' && r.host ? { host: r.host } : {}),
    ...(r.fallback === true ? { fallback: true } : {}),
    ...(attempts.length > 0 ? { failedAttempts: attempts } : {}),
  };
}

function routingLines(routing: RoutingInfo): string[] {
  const lines = [`endpoint: ${routing.endpointId}`];
  if (routing.provider && routing.provider !== routing.endpointId) {
    lines.push(`provider: ${routing.provider}`);
  }
  if (routing.host) lines.push(`host: ${routing.host}`);
  if (routing.fallback) lines.push('served by fallback');
  for (const attempt of routing.failedAttempts ?? []) {
    lines.push(`failed: ${attempt.endpointId || attempt.provider} (${attempt.errorType})`);
  }
  return lines;
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied: fall through to the legacy path.
  }
  // A plain-http console (a LAN address) has no async clipboard at all.
  const el = document.createElement('textarea');
  el.value = text;
  el.setAttribute('readonly', '');
  el.style.position = 'fixed';
  el.style.opacity = '0';
  document.body.appendChild(el);
  el.select();
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    el.remove();
  }
}

// Hover-revealed controls must still show for keyboard focus and on touch screens.
const REVEAL_ON_HOVER =
  'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100';

export default function PlaygroundPage() {
  const { state } = useAuth();
  const branding = useBranding();
  const [models, setModels] = useState<PlaygroundModel[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<PlaygroundSession[]>([createSession('s-1')]);
  const [activeSessionId, setActiveSessionId] = useState('s-1');
  // Sessions with a turn in flight. Streaming is scoped per session (issue:
  // "streaming locks the entire UI"): other sessions stay fully usable —
  // switching, editing, deleting, and starting their own streams — while a
  // session streams. Every turn owns its scratch state (pending deltas, raf
  // handle, timing, abort controller) as locals, so parallel turns cannot
  // overwrite each other.
  const [streamingIds, setStreamingIds] = useState<ReadonlySet<string>>(() => new Set());
  const [copyState, setCopyState] = useState<{ id: string; ok: boolean } | null>(null);
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(new Set());
  const [expandedRouting, setExpandedRouting] = useState<Set<string>>(new Set());
  const [showJump, setShowJump] = useState(false);
  const [undo, setUndo] = useState<UndoAction | null>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const counterRef = useRef(2);
  const followRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  // Which session is on screen right now, as a ref so an animation-frame
  // callback reads the current answer rather than the one captured when the
  // stream began: the user can switch sessions mid-stream.
  const visibleSessionRef = useRef<string>(activeSessionId);

  useEffect(() => {
    visibleSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  // Navigating away must not leave generations running until the network or
  // idle timeout ends them: every live abort controller fires on unmount. The
  // per-turn `finally` stays authoritative for its own scratch state (raf
  // cancel, streaming-set removal, aborted stamp) — this only pulls the
  // network plug.
  useEffect(() => {
    const controllers = abortControllersRef.current;
    return () => {
      for (const controller of controllers.values()) {
        controller.abort();
      }
      controllers.clear();
    };
  }, []);

  const session = sessions.find((s) => s.id === activeSessionId) ?? sessions[0];
  const modelId = session?.selectedModelId || '';
  const sysPrompt = session?.systemPrompt || '';
  const temp = session?.temperature ?? 0.7;
  const maxTok = session?.maxTokens ?? 4096;
  const msgs = session?.messages ?? NO_MESSAGES;
  const input = session?.input || '';
  const model = models.find((m) => m.id === modelId) ?? null;
  const selectedProvider = session?.selectedProvider ?? null;
  const modelProviders = model?.providers ?? [];

  const streaming = streamingIds.size > 0;
  const activeStreaming = streamingIds.has(session?.id ?? '');

  const patchSession = useCallback(
    (sessionId: string, fn: (s: PlaygroundSession) => PlaygroundSession) => {
      setSessions((prev) => prev.map((s) => (s.id === sessionId ? fn(s) : s)));
    },
    [],
  );

  const patch = useCallback(
    (fn: (s: PlaygroundSession) => PlaygroundSession) => {
      patchSession(activeSessionId, fn);
    },
    [activeSessionId, patchSession],
  );

  const loadModels = useCallback(() => {
    setModelsLoading(true);
    setModelsError(null);
    fetchWithAuth(API_BASE, '/internal/playground/models')
      .then((r) => jsonOrThrow<{ models: PlaygroundModel[] }>(r))
      .then((d) => {
        setModels(d.models);
        if (d.models.length > 0) {
          setSessions((prev) =>
            prev.map((s) => (s.selectedModelId ? s : { ...s, selectedModelId: d.models[0].id })),
          );
        }
      })
      .catch((err: unknown) => {
        setModelsError(err instanceof Error && err.message ? err.message : 'Request failed');
      })
      .finally(() => setModelsLoading(false));
  }, []);

  useEffect(() => {
    if (!hasRole(state.user?.role, 'internal')) return;
    loadModels();
  }, [state.user?.role, loadModels]);

  // The countdown waits for *its* session's stream to finish: Undo is
  // disabled until then. Scoped to the Undo's own session, so a stream in
  // another session neither blocks nor restarts this timer.
  const undoPaused = undo !== null && streamingIds.has(undo.sessionId);
  useEffect(() => {
    if (!undo || undoPaused) return;
    const timer = setTimeout(() => setUndo(null), UNDO_MS);
    return () => clearTimeout(timer);
  }, [undo, undoPaused]);

  /** Scroll to the newest message, unless the reader has scrolled away (`force` overrides). */
  const scrollDown = useCallback((behavior: ScrollBehavior = 'auto', force = false) => {
    const el = chatRef.current;
    if (!el || (!force && !followRef.current)) return;
    followRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  const onChatScroll = useCallback(() => {
    const el = chatRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_THRESHOLD;
    // Only an upward move detaches: growing content and smooth scrolls never do.
    if (atBottom) followRef.current = true;
    else if (el.scrollTop < lastScrollTopRef.current) followRef.current = false;
    lastScrollTopRef.current = el.scrollTop;
    setShowJump(!followRef.current);
  }, []);

  /** Deltas buffered for one in-flight turn, flushed in animation frames. */
  interface TurnDeltas {
    content: string;
    reasoning: string;
    routing: RoutingInfo | null;
    raf: number | null;
  }

  const flushDelta = useCallback(
    (sessionId: string, turn: TurnDeltas) => {
      if (!turn.content && !turn.reasoning && !turn.routing) return;
      const contentDelta = turn.content;
      const reasoningDelta = turn.reasoning;
      const routing = turn.routing;
      turn.content = '';
      turn.reasoning = '';
      turn.routing = null;
      patchSession(sessionId, (s) => {
        const copy = [...s.messages];
        const last = copy[copy.length - 1];
        if (!last) return s;
        copy[copy.length - 1] = {
          ...last,
          ...(contentDelta ? { content: last.content + contentDelta } : {}),
          ...(reasoningDelta
            ? { reasoningContent: (last.reasoningContent || '') + reasoningDelta }
            : {}),
          ...(routing ? { routing } : {}),
        };
        return { ...s, messages: copy };
      });
    },
    [patchSession],
  );

  const scheduleFlush = useCallback(
    (sessionId: string, turn: TurnDeltas) => {
      if (turn.raf !== null) return;
      turn.raf = requestAnimationFrame(() => {
        turn.raf = null;
        flushDelta(sessionId, turn);
        // Only the session the reader is looking at follows the stream: a
        // background turn still lands its deltas in its own session, but it
        // must not yank the visible one's scroll position. The ref, not the
        // value captured when the stream began, because the user can switch
        // sessions mid-generation.
        if (visibleSessionRef.current === sessionId) {
          scrollDown('auto');
        }
      });
    },
    [flushDelta, scrollDown],
  );

  useEffect(() => {
    scrollDown('auto', true);
  }, [activeSessionId, scrollDown]);

  useEffect(() => {
    scrollDown(activeStreaming ? 'auto' : 'smooth');
  }, [msgs.length, scrollDown, activeStreaming]);

  // Runs on every input change, including the clear after a send and session switches.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input, activeSessionId]);

  const handleInputChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      patch((s) => ({ ...s, input: e.target.value }));
    },
    [patch],
  );

  /**
   * Put `messages` back, but only while the session still holds exactly what
   * the undone action left (`expected`): anything said since must not be lost.
   */
  const restoreMessages = useCallback(
    (sessionId: string, messages: Message[], draft: string, expected?: Message[]) => {
      setSessions((prev) =>
        prev.map((s) =>
          s.id === sessionId && (!expected || s.messages === expected)
            ? { ...s, messages, input: s.input || draft }
            : s,
        ),
      );
    },
    [],
  );

  const resetSession = useCallback(() => {
    if (!session) return;
    const wasStreaming = streamingIds.has(session.id);
    const snapshot = session.messages.map((m, i, all) =>
      wasStreaming && i === all.length - 1 && m.role === 'assistant' && !m.status
        ? { ...m, status: 'aborted' as const }
        : m,
    );
    const draft = session.input;
    const sessionId = session.id;
    // Aborting is all the turn needs: it owns its scratch state, and its
    // finally stamps the aborted placeholder — a no-op on the cleared
    // session, whose messages are already gone.
    if (wasStreaming) abortControllersRef.current.get(sessionId)?.abort();
    const cleared: Message[] = [];
    patchSession(sessionId, (s) => ({ ...s, messages: cleared, input: '' }));
    if (snapshot.length > 0 || draft) {
      setUndo({
        label: 'Conversation cleared',
        sessionId,
        restore: () => restoreMessages(sessionId, snapshot, draft, cleared),
      });
    }
  }, [session, streamingIds, patchSession, restoreMessages]);

  const newSession = useCallback(() => {
    const id = `s-${counterRef.current++}`;
    // Carry the current settings over, so comparing runs doesn't mean redoing them.
    const ns: PlaygroundSession = {
      ...createSession(id, modelId || models[0]?.id || ''),
      selectedProvider,
      systemPrompt: sysPrompt,
      temperature: temp,
      maxTokens: maxTok,
    };
    setSessions((p) => [ns, ...p]);
    setActiveSessionId(id);
  }, [models, modelId, selectedProvider, sysPrompt, temp, maxTok]);

  const deleteSession = useCallback(
    (id: string) => {
      // The session a turn is streaming into cannot be deleted out from under
      // it; every other session deletes freely, even while something streams.
      if (streamingIds.has(id) || sessions.length <= 1) return;
      const index = sessions.findIndex((s) => s.id === id);
      if (index < 0) return;
      const removed = sessions[index];
      const remaining = sessions.filter((s) => s.id !== id);
      setSessions(remaining);
      if (id === activeSessionId) {
        setActiveSessionId(remaining[Math.min(index, remaining.length - 1)].id);
      }
      setUndo({
        label: `Deleted "${getSessionTitle(removed)}"`,
        sessionId: id,
        restore: () => {
          setSessions((prev) =>
            prev.some((s) => s.id === id)
              ? prev
              : [...prev.slice(0, index), removed, ...prev.slice(index)],
          );
          setActiveSessionId(id);
        },
      });
    },
    [activeSessionId, sessions, streamingIds],
  );

  const switchSession = useCallback(
    (id: string) => {
      if (id === activeSessionId) return;
      // Nothing to detach: a background turn writes into its own session, so
      // switching the view never drops or misroutes its deltas.
      setActiveSessionId(id);
    },
    [activeSessionId],
  );

  const handleCopy = useCallback(async (text: string, id: string) => {
    const ok = await copyToClipboard(text);
    setCopyState({ id, ok });
    setTimeout(() => setCopyState((cur) => (cur?.id === id ? null : cur)), 1500);
  }, []);

  const toggleIn = (setter: typeof setExpandedThinking, id: string) => {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  /**
   * Stream a reply to `base`, which ends with the user message being answered.
   * A new turn retires any pending Undo; `undo` replaces it when the turn
   * itself is the undoable action. Everything the turn mutates — its buffered
   * deltas, raf handle, timing, abort controller — is a local, so turns run
   * per session and cannot overwrite each other's state.
   */
  const startTurn = useCallback(
    async (base: Message[], undo: UndoAction | null = null) => {
      const turnSessionId = activeSessionId;
      if (streamingIds.has(turnSessionId) || !modelId) return;
      const turn: TurnDeltas = { content: '', reasoning: '', routing: null, raf: null };
      const timing = {
        start: 0,
        firstToken: 0,
        contentStart: 0,
        tokens: 0,
        finishReason: undefined as string | undefined,
      };

      setUndo(undo);
      followRef.current = true;
      patchSession(turnSessionId, (s) => ({
        ...s,
        title: getSessionTitle({ ...s, messages: base }),
        messages: [
          ...base,
          {
            id: messageId(),
            role: 'assistant',
            content: '',
            reasoningContent: '',
            modelName: model?.name,
          },
        ],
      }));
      setStreamingIds((prev) => new Set(prev).add(turnSessionId));
      timing.start = performance.now();
      let failure: { status: 'error' | 'aborted'; error?: string } | null = null;

      const ctrl = new AbortController();
      abortControllersRef.current.set(turnSessionId, ctrl);
      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const resetIdle = () => {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          if (!failure) {
            failure = {
              status: 'error',
              error: `Lost the connection: nothing from the server for ${STREAM_IDLE_MS / 1000} s.`,
            };
          }
          ctrl.abort();
        }, STREAM_IDLE_MS);
      };
      resetIdle();

      try {
        const resp = await fetchWithAuth(API_BASE, '/internal/playground/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model: modelId,
            system_prompt: sysPrompt,
            messages: requestHistory(base),
            temperature: temp,
            max_tokens: maxTok,
            ...(selectedProvider ? { provider: selectedProvider } : {}),
          }),
          signal: ctrl.signal,
        });

        // Throws with the backend's own message rather than its raw JSON body.
        if (!resp.ok) await jsonOrThrow(resp);

        const reader = resp.body?.getReader();
        if (!reader) throw new Error('The response had no body.');

        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          let chunk: ReadableStreamReadResult<Uint8Array>;
          try {
            chunk = await reader.read();
          } catch (err) {
            if ((err as Error).name === 'AbortError') throw err;
            // The browser's own wording ("network error", "Error in input
            // stream") says nothing about what happened: the server went away
            // in the middle of the reply.
            throw new Error('Lost the connection to the server mid-reply.');
          }
          const { done, value } = chunk;
          if (done) break;
          resetIdle();
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop() || '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const payload = line.slice(6).trim();
            if (payload === '[DONE]') continue;
            try {
              const parsed = JSON.parse(payload);
              if (parsed.usage?.completion_tokens) {
                timing.tokens = parsed.usage.completion_tokens;
              }
              const inBandError = frameError(parsed.error);
              if (inBandError) failure = { status: 'error', error: inBandError };
              // The router announces one routing frame per attempt, so a
              // fallback overwrites the primary — last one wins is the one
              // that actually served the response.
              const routing = parseRouting(parsed._playground_route);
              if (routing) {
                turn.routing = routing;
                scheduleFlush(turnSessionId, turn);
              }
              const choice = parsed.choices?.[0];
              if (choice?.finish_reason) timing.finishReason = choice.finish_reason;
              const delta = choice?.delta;
              if (delta?.content || delta?.reasoning_content) {
                const now = performance.now();
                if (!timing.firstToken) timing.firstToken = now;
                if (delta.content && !timing.contentStart) timing.contentStart = now;
              }
              if (delta?.content) {
                turn.content += delta.content;
                scheduleFlush(turnSessionId, turn);
              }
              if (delta?.reasoning_content) {
                turn.reasoning += delta.reasoning_content;
                scheduleFlush(turnSessionId, turn);
              }
            } catch {
              /* skip */
            }
          }
          if (failure) {
            // An in-band error ends the turn even if the connection stays open.
            ctrl.abort();
            break;
          }
        }
        if (!failure && !timing.firstToken) {
          failure = { status: 'error', error: 'The model returned an empty response.' };
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          failure = { status: 'error', error: (err as Error).message || 'Request failed' };
        } else if (!failure) {
          // Stop after an upstream error keeps the error, so the turn stays out of context.
          failure = { status: 'aborted' };
        }
      } finally {
        clearTimeout(idleTimer);
        if (turn.raf !== null) {
          cancelAnimationFrame(turn.raf);
          turn.raf = null;
        }
        flushDelta(turnSessionId, turn);
        const start = timing.start;
        const end = performance.now();
        const firstToken = timing.firstToken;
        const contentStart = timing.contentStart;
        const tokens = timing.tokens || undefined;
        const finishReason = timing.finishReason;
        const outcome = failure;
        patchSession(turnSessionId, (s) => {
          const last = s.messages[s.messages.length - 1];
          // Cleared mid-stream: leave the session (and its Undo) untouched.
          if (last?.role !== 'assistant') return s;
          const cleaned: Message = {
            ...last,
            durationMs: Math.round(end - start),
            ttftMs: firstToken ? Math.round(firstToken - start) : undefined,
            completionTokens: tokens,
            ...(finishReason ? { finishReason } : {}),
            ...(outcome ?? {}),
          };
          if (cleaned.reasoningContent && firstToken) {
            cleaned.reasoningMs = Math.round((contentStart || end) - firstToken);
          } else {
            delete cleaned.reasoningContent;
          }
          return { ...s, messages: [...s.messages.slice(0, -1), cleaned] };
        });
        abortControllersRef.current.delete(turnSessionId);
        setStreamingIds((prev) => {
          const next = new Set(prev);
          next.delete(turnSessionId);
          return next;
        });
      }
    },
    [
      activeSessionId,
      streamingIds,
      modelId,
      model?.name,
      sysPrompt,
      temp,
      maxTok,
      selectedProvider,
      patchSession,
      flushDelta,
      scheduleFlush,
    ],
  );

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || activeStreaming || !modelId) return;
    patch((s) => ({ ...s, input: '' }));
    void startTurn([...msgs, { id: messageId(), role: 'user', content: text }]);
  }, [input, activeStreaming, modelId, msgs, patch, startTurn]);

  /** Re-run the last reply: a retry after a failure, a fresh sample otherwise. */
  const regenerate = useCallback(() => {
    const last = msgs[msgs.length - 1];
    if (activeStreaming || last?.role !== 'assistant' || !session) return;
    const sessionId = session.id;
    const snapshot = msgs;
    void startTurn(
      msgs.slice(0, -1),
      isUsableReply(last)
        ? {
            label: 'Replaced the last reply',
            sessionId,
            restore: () => restoreMessages(sessionId, snapshot, ''),
          }
        : null,
    );
  }, [msgs, activeStreaming, session, restoreMessages, startTurn]);

  /** Move the last question back into the editor, dropping its reply. */
  const editLast = useCallback(() => {
    if (activeStreaming || !session) return;
    const index = msgs.map((m) => m.role).lastIndexOf('user');
    if (index < 0) return;
    const sessionId = session.id;
    const snapshot = msgs;
    const draft = input;
    const remaining = msgs.slice(0, index);
    setSessions((prev) =>
      prev.map((s) =>
        s.id === sessionId ? { ...s, messages: remaining, input: msgs[index].content } : s,
      ),
    );
    setUndo({
      label: 'Moved your last message back to the editor',
      sessionId,
      restore: () =>
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sessionId && s.messages === remaining
              ? { ...s, messages: snapshot, input: draft }
              : s,
          ),
        ),
    });
    textareaRef.current?.focus();
  }, [msgs, input, activeStreaming, session]);

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      send();
    }
  };

  if (state.loading) {
    return (
      <div className="flex h-full items-center justify-center bg-gray-950">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-700 border-t-white" />
      </div>
    );
  }

  if (!hasRole(state.user?.role, 'internal')) {
    return (
      <ProtectedRoute>
        <div className="flex h-full flex-col items-center justify-center gap-4 bg-gray-950 text-white">
          <h1 className="text-2xl font-semibold">Admin Access Required</h1>
          <p className="text-sm text-gray-400">This page is only available to administrators.</p>
          <Link
            href="/dashboard"
            className="text-sm font-medium text-indigo-400 hover:text-indigo-300"
          >
            Back to Dashboard
          </Link>
        </div>
      </ProtectedRoute>
    );
  }

  const lastUserIndex = msgs.map((m) => m.role).lastIndexOf('user');

  return (
    <ProtectedRoute>
      <div className="flex h-full flex-col bg-gray-950 text-white">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-gray-800 px-5">
          <div className="flex items-center gap-4">
            <Link
              href="/dashboard"
              className="text-sm font-semibold text-gray-400 transition hover:text-white"
            >
              {branding.appName}
            </Link>
            <span className="text-gray-700">/</span>
            <span className="text-sm font-medium text-white">Playground</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="rounded-md bg-gray-800 px-2.5 py-1 text-xs font-medium text-gray-300">
              {model ? model.name : 'No model'}
            </span>
            <span
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                streaming ? 'bg-indigo-600/20 text-indigo-400' : 'bg-gray-800 text-gray-500'
              }`}
            >
              {streaming ? 'Streaming' : 'Ready'}
            </span>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <aside className="flex w-80 shrink-0 flex-col border-r border-gray-800 bg-gray-900">
            <div className="border-b border-gray-800 px-4 py-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                  Sessions
                </span>
                <button
                  type="button"
                  onClick={newSession}
                  className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500 disabled:opacity-50"
                >
                  + New
                </button>
              </div>
              <div className="dark-scrollbar max-h-[200px] space-y-1 overflow-y-auto">
                {sessions.map((s) => {
                  const active = s.id === activeSessionId;
                  return (
                    <div
                      key={s.id}
                      className={`group relative rounded-lg transition ${
                        active
                          ? 'bg-gray-800 text-white'
                          : 'text-gray-400 hover:bg-gray-800/60 hover:text-gray-200'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => switchSession(s.id)}
                        className="w-full px-3 py-2.5 text-left text-sm"
                      >
                        <div className="truncate pr-6 font-medium">{getSessionTitle(s)}</div>
                        <div className="mt-1 truncate text-xs text-gray-600">
                          {getMessageCount(s.messages)} -- {s.selectedModelId || 'no model'}
                        </div>
                      </button>
                      {sessions.length > 1 && !streamingIds.has(s.id) && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteSession(s.id);
                          }}
                          className={`absolute right-2 top-2.5 rounded p-1 text-gray-600 transition hover:bg-gray-700 hover:text-gray-300 ${REVEAL_ON_HOVER}`}
                          title="Delete session"
                          aria-label={`Delete session ${getSessionTitle(s)}`}
                        >
                          <svg
                            aria-hidden="true"
                            className="h-3.5 w-3.5"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth={2}
                              d="M6 18L18 6M6 6l12 12"
                            />
                          </svg>
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="dark-scrollbar flex-1 overflow-y-auto px-4 py-4">
              <div className="space-y-5">
                <div>
                  <label
                    htmlFor="playground-model"
                    className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500"
                  >
                    Model
                  </label>
                  <ModelPicker
                    key={activeSessionId}
                    id="playground-model"
                    models={models}
                    value={modelId}
                    onChange={(newId) => {
                      patch((s) => ({
                        ...s,
                        selectedModelId: newId,
                        selectedProvider: null,
                      }));
                    }}
                    disabled={activeStreaming}
                    loading={modelsLoading}
                    emptyLabel={modelsError ? 'Models unavailable' : undefined}
                  />
                  {modelsError && !modelsLoading && (
                    <div className="mt-2 flex items-start justify-between gap-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                      <span className="min-w-0 break-words">
                        Couldn&apos;t load models: {modelsError}
                      </span>
                      <button
                        type="button"
                        onClick={loadModels}
                        className="shrink-0 font-medium text-red-200 underline-offset-2 hover:underline"
                      >
                        Retry
                      </button>
                    </div>
                  )}
                </div>

                {modelProviders.length > 1 && (
                  <div>
                    <label
                      htmlFor="playground-provider"
                      className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500"
                    >
                      Provider
                    </label>
                    <Select
                      id="playground-provider"
                      tone="dark"
                      size="lg"
                      className="w-full"
                      value={selectedProvider ?? ''}
                      onValueChange={(value) =>
                        patch((s) => ({
                          ...s,
                          selectedProvider: value || null,
                        }))
                      }
                      disabled={activeStreaming}
                      options={[
                        { value: '', label: 'Auto' },
                        ...modelProviders.map((p) => ({ value: p.id, label: p.name })),
                      ]}
                    />
                  </div>
                )}

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label
                      htmlFor="playground-temperature"
                      className="text-xs font-semibold uppercase tracking-wider text-gray-500"
                    >
                      Temperature
                    </label>
                    <span className="text-xs tabular-nums text-gray-400">{temp.toFixed(1)}</span>
                  </div>
                  <input
                    id="playground-temperature"
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={temp}
                    onChange={(e) =>
                      patch((s) => ({ ...s, temperature: parseFloat(e.target.value) }))
                    }
                    disabled={activeStreaming}
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-700 accent-indigo-500 disabled:opacity-50"
                  />
                </div>

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label
                      htmlFor="playground-max-tokens"
                      className="text-xs font-semibold uppercase tracking-wider text-gray-500"
                    >
                      Max Tokens
                    </label>
                    <span className="text-xs tabular-nums text-gray-400">{maxTok}</span>
                  </div>
                  <input
                    id="playground-max-tokens"
                    type="range"
                    min="256"
                    max="32768"
                    step="256"
                    value={maxTok}
                    onChange={(e) =>
                      patch((s) => ({ ...s, maxTokens: parseInt(e.target.value, 10) }))
                    }
                    disabled={activeStreaming}
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-700 accent-indigo-500 disabled:opacity-50"
                  />
                </div>

                <div>
                  <label
                    htmlFor="playground-system"
                    className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500"
                  >
                    System instructions
                  </label>
                  <textarea
                    id="playground-system"
                    value={sysPrompt}
                    onChange={(e) => patch((s) => ({ ...s, systemPrompt: e.target.value }))}
                    placeholder="You are a research assistant..."
                    rows={6}
                    disabled={activeStreaming}
                    className="w-full resize-none rounded-lg border border-gray-700 bg-gray-800 px-3 py-2.5 text-sm leading-6 text-white outline-none transition placeholder:text-gray-600 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
                  />
                </div>

                <button
                  type="button"
                  onClick={resetSession}
                  className="w-full rounded-lg border border-gray-700 px-3 py-2 text-sm font-medium text-gray-400 transition hover:border-gray-600 hover:text-gray-200"
                >
                  Clear conversation
                </button>
              </div>
            </div>
          </aside>

          <div className="flex min-w-0 flex-1 flex-col bg-gray-950">
            <div
              ref={chatRef}
              onScroll={onChatScroll}
              className="dark-scrollbar flex-1 overflow-y-auto px-6 py-6"
            >
              {msgs.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="max-w-lg text-center">
                    <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-gray-800">
                      <svg
                        className="h-6 w-6 text-gray-500"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={1.5}
                          d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
                        />
                      </svg>
                    </div>
                    <h2 className="text-xl font-semibold text-white">Start a chat run</h2>
                    <p className="mt-2 text-sm leading-6 text-gray-500">
                      Choose a model and system prompt in the sidebar, then type a message below.
                    </p>
                  </div>
                </div>
              ) : (
                <div className="mx-auto max-w-3xl space-y-6">
                  {msgs.map((msg, i) => {
                    const isUser = msg.role === 'user';
                    const isLast = i === msgs.length - 1;
                    const isStreamingThis = activeStreaming && isLast && !isUser;
                    const isWaiting =
                      isStreamingThis && !msg.content && !msg.reasoningContent && !msg.status;
                    const copied = copyState?.id === msg.id ? copyState : null;
                    const hasReasoning = !!msg.reasoningContent;
                    const isThinkingExpanded = expandedThinking.has(msg.id);
                    const isRoutingExpanded = expandedRouting.has(msg.id);
                    const rate = decodeRate(msg);
                    const note = finishNote(msg);
                    const canRetry = !isUser && isLast && !activeStreaming;
                    const canEdit = isUser && i === lastUserIndex && !activeStreaming;

                    return (
                      <div key={msg.id} className="group">
                        <div className="mb-1.5 flex items-center justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-2">
                            <span
                              className={`text-xs font-medium ${
                                isUser ? 'text-indigo-400' : 'text-gray-500'
                              }`}
                            >
                              {isUser ? 'You' : msg.modelName || model?.name || 'Assistant'}
                            </span>
                            {!isUser && msg.routing && (
                              <button
                                type="button"
                                onClick={() => toggleIn(setExpandedRouting, msg.id)}
                                aria-expanded={isRoutingExpanded}
                                className={`truncate rounded px-1.5 py-0.5 text-[11px] font-medium transition hover:brightness-125 ${
                                  msg.routing.fallback
                                    ? 'bg-amber-500/10 text-amber-400'
                                    : 'bg-gray-800 text-gray-400'
                                }`}
                                title={routingLines(msg.routing).join('\n')}
                              >
                                {msg.routing.fallback ? '↳ ' : ''}
                                {msg.routing.endpointId}
                              </button>
                            )}
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            {!isUser && msg.durationMs != null && !isStreamingThis && (
                              <span className="text-xs tabular-nums text-gray-500">
                                {msg.ttftMs != null && `${msg.ttftMs}ms TTFT · `}
                                {rate != null && `${rate.toFixed(1)} tok/s · `}
                                {formatDuration(msg.durationMs)}
                              </span>
                            )}
                            {canEdit && (
                              <button
                                type="button"
                                onClick={editLast}
                                className={`rounded px-1.5 py-0.5 text-xs text-gray-500 transition hover:bg-gray-800 hover:text-gray-200 ${REVEAL_ON_HOVER}`}
                              >
                                Edit
                              </button>
                            )}
                            {canRetry && (
                              <button
                                type="button"
                                onClick={regenerate}
                                disabled={!modelId}
                                className={`rounded px-1.5 py-0.5 text-xs transition hover:bg-gray-800 disabled:opacity-40 ${
                                  msg.status || note
                                    ? 'text-gray-300 hover:text-white'
                                    : `text-gray-500 hover:text-gray-200 ${REVEAL_ON_HOVER}`
                                }`}
                              >
                                {msg.status ? 'Retry' : 'Regenerate'}
                              </button>
                            )}
                            {msg.content && !isStreamingThis && (
                              <button
                                type="button"
                                onClick={() => void handleCopy(msg.content, msg.id)}
                                className={`rounded p-1 text-gray-600 transition hover:bg-gray-800 hover:text-gray-300 ${REVEAL_ON_HOVER}`}
                                title={
                                  copied
                                    ? copied.ok
                                      ? 'Copied'
                                      : 'Copy failed'
                                    : 'Copy to clipboard'
                                }
                                aria-label={
                                  copied ? (copied.ok ? 'Copied' : 'Copy failed') : 'Copy message'
                                }
                              >
                                {copied?.ok ? (
                                  <svg
                                    aria-hidden="true"
                                    className="h-3.5 w-3.5 text-green-500"
                                    fill="none"
                                    viewBox="0 0 24 24"
                                    stroke="currentColor"
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      strokeWidth={2}
                                      d="M5 13l4 4L19 7"
                                    />
                                  </svg>
                                ) : copied ? (
                                  <svg
                                    aria-hidden="true"
                                    className="h-3.5 w-3.5 text-red-400"
                                    fill="none"
                                    viewBox="0 0 24 24"
                                    stroke="currentColor"
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      strokeWidth={2}
                                      d="M6 18L18 6M6 6l12 12"
                                    />
                                  </svg>
                                ) : (
                                  <svg
                                    aria-hidden="true"
                                    className="h-3.5 w-3.5"
                                    fill="none"
                                    viewBox="0 0 24 24"
                                    stroke="currentColor"
                                  >
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      strokeWidth={2}
                                      d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
                                    />
                                  </svg>
                                )}
                              </button>
                            )}
                          </div>
                        </div>
                        {!isUser && msg.routing && isRoutingExpanded && (
                          <ul className="mb-2 space-y-0.5 rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2 font-mono text-[11px] leading-5 text-gray-400">
                            {routingLines(msg.routing).map((line) => (
                              <li key={line} className="break-all">
                                {line}
                              </li>
                            ))}
                          </ul>
                        )}
                        <div
                          className={`rounded-2xl px-5 py-4 text-sm leading-7 ${
                            isUser
                              ? 'bg-indigo-600 text-white'
                              : 'bg-gray-900 text-gray-200 ring-1 ring-gray-800'
                          }`}
                        >
                          {isWaiting ? (
                            <div className="flex items-center gap-2.5 text-gray-500">
                              <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-700 border-t-gray-400" />
                              <span className="text-xs">Waiting for the first token...</span>
                            </div>
                          ) : (
                            <>
                              {hasReasoning && (
                                <div className="mb-3">
                                  <button
                                    type="button"
                                    onClick={() => toggleIn(setExpandedThinking, msg.id)}
                                    aria-expanded={isThinkingExpanded}
                                    className="flex items-center gap-1.5 text-xs text-gray-500 transition hover:text-gray-300"
                                  >
                                    <svg
                                      aria-hidden="true"
                                      className={`h-3 w-3 transition-transform ${
                                        isThinkingExpanded ? 'rotate-90' : ''
                                      }`}
                                      fill="none"
                                      viewBox="0 0 24 24"
                                      stroke="currentColor"
                                    >
                                      <path
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        strokeWidth={2}
                                        d="M9 5l7 7-7 7"
                                      />
                                    </svg>
                                    <span>
                                      {isStreamingThis && !msg.content
                                        ? 'Thinking...'
                                        : msg.reasoningMs != null
                                          ? `Thought for ${formatDuration(msg.reasoningMs)}`
                                          : 'Reasoning'}
                                    </span>
                                  </button>
                                  {isThinkingExpanded && (
                                    <div className="mt-2 rounded-lg border border-gray-800 bg-gray-950 px-4 py-3 text-xs leading-6 text-gray-500 whitespace-pre-wrap">
                                      {msg.reasoningContent}
                                    </div>
                                  )}
                                </div>
                              )}
                              {isUser ? (
                                <div className="whitespace-pre-wrap">{msg.content}</div>
                              ) : (
                                msg.content && <Markdown text={msg.content} tone="dark" />
                              )}
                              {msg.status === 'error' && (
                                <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-300">
                                  <p className="break-words">{`Error: ${msg.error}`}</p>
                                  <p className="mt-0.5 text-red-300/70">
                                    This turn is left out of the context for later messages.
                                  </p>
                                </div>
                              )}
                              {msg.status === 'aborted' && (
                                <p className="mt-2 text-xs text-gray-500">
                                  {msg.content
                                    ? 'Stopped. The partial reply is kept as context.'
                                    : 'Stopped before any output.'}
                                </p>
                              )}
                              {note && (
                                <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-300">
                                  {note}
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="relative shrink-0 border-t border-gray-800 bg-gray-950 px-6 py-4">
              {showJump && msgs.length > 0 && (
                <button
                  type="button"
                  onClick={() => scrollDown('smooth', true)}
                  className="absolute bottom-full left-1/2 mb-3 -translate-x-1/2 rounded-full border border-gray-700 bg-gray-900/95 px-3 py-1.5 text-xs font-medium text-gray-200 shadow-lg shadow-black/40 transition hover:border-gray-500 hover:text-white"
                >
                  ↓ Jump to latest
                </button>
              )}
              <div className="mx-auto max-w-3xl">
                {undo && (
                  <div
                    role="status"
                    className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-xs text-gray-300"
                  >
                    <span className="min-w-0 truncate">{undo.label}</span>
                    <div className="flex shrink-0 items-center gap-3">
                      <button
                        type="button"
                        disabled={undoPaused}
                        onClick={() => {
                          undo.restore();
                          setUndo(null);
                        }}
                        className="font-medium text-indigo-400 hover:text-indigo-300 disabled:opacity-40"
                      >
                        Undo
                      </button>
                      <button
                        type="button"
                        onClick={() => setUndo(null)}
                        aria-label="Dismiss"
                        className="text-gray-500 hover:text-gray-300"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                )}
                <div className="rounded-2xl border border-gray-800 bg-gray-900 p-3">
                  <textarea
                    ref={textareaRef}
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={onKey}
                    aria-label="Message"
                    placeholder="Type a message... (Ctrl+Enter to send)"
                    rows={1}
                    className="max-h-[200px] min-h-[44px] w-full resize-none bg-transparent px-2 py-2 text-sm leading-6 text-white outline-none placeholder:text-gray-600"
                  />
                  <div className="flex items-center justify-between border-t border-gray-800 px-2 pt-3">
                    <div className="text-xs text-gray-600">
                      {(() => {
                        if (!model) return modelsLoading ? 'Loading...' : 'No model selected';
                        const parts = [model.name];
                        if (selectedProvider) parts.push(selectedProvider);
                        parts.push(`temp ${temp.toFixed(1)}`);
                        parts.push(`max ${maxTok}`);
                        if (sysPrompt.trim()) parts.push('custom instructions');
                        return parts.join(' / ');
                      })()}
                    </div>
                    <div className="flex items-center gap-2">
                      {activeStreaming && (
                        <button
                          type="button"
                          onClick={() => {
                            if (session) abortControllersRef.current.get(session.id)?.abort();
                          }}
                          className="rounded-lg border border-gray-700 px-3 py-1.5 text-xs font-medium text-gray-400 transition hover:border-gray-600 hover:text-white"
                        >
                          Stop
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={send}
                        disabled={activeStreaming || !input.trim() || !modelId}
                        className="rounded-lg bg-indigo-600 px-4 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500 disabled:opacity-40"
                      >
                        {activeStreaming ? 'Sending...' : 'Send'}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </ProtectedRoute>
  );
}
