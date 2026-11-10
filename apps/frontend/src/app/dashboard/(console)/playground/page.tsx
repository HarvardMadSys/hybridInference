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
import { hasRole } from '@/components/providers/AuthProvider';
import { Markdown } from '@/components/ui/Markdown';
import { Select } from '@/components/ui/Select';
import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '@/lib/api/client';
import { ModelPicker, type PlaygroundModel } from './ModelPicker';
import {
  API_OPTIONS,
  buildCode,
  buildRequest,
  endpointFor,
  parseStreamEvent,
  type PlaygroundApi,
} from './api';
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
  api: PlaygroundApi;
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
    api: 'chat',
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
  // Below `lg` the settings rail is a drawer over the conversation, opened
  // from the header; from `lg` up it is the panel beside it.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [codeOpen, setCodeOpen] = useState(false);
  const [codeCopied, setCodeCopied] = useState(false);
  const settingsButtonRef = useRef<HTMLButtonElement>(null);
  const settingsCloseRef = useRef<HTMLButtonElement>(null);
  const codeButtonRef = useRef<HTMLButtonElement>(null);
  const codeCloseRef = useRef<HTMLButtonElement>(null);
  const chatRef = useRef<HTMLElement>(null);
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

  useEffect(() => {
    if (!settingsOpen) return;
    settingsCloseRef.current?.focus();
    const onKey = (event: globalThis.KeyboardEvent) => {
      // A picker inside the drawer consumes its own Escape.
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      setSettingsOpen(false);
      settingsButtonRef.current?.focus();
    };
    // Widened past `lg`, the panel is back in place: drop the drawer rather
    // than have it reappear over the conversation on the next narrowing.
    const wide =
      typeof window.matchMedia === 'function' ? window.matchMedia('(min-width: 1024px)') : null;
    const onWidth = () => {
      if (wide?.matches) setSettingsOpen(false);
    };
    document.addEventListener('keydown', onKey);
    wide?.addEventListener('change', onWidth);
    return () => {
      document.removeEventListener('keydown', onKey);
      wide?.removeEventListener('change', onWidth);
    };
  }, [settingsOpen]);

  useEffect(() => {
    if (!codeOpen) return;
    codeCloseRef.current?.focus();
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setCodeOpen(false);
      codeButtonRef.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [codeOpen]);

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
  const sessionApi = session?.api ?? 'chat';
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
      const api = session?.api ?? 'chat';
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
        const resp = await fetchWithAuth(API_BASE, endpointFor(api), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(
            buildRequest(api, {
              model: modelId,
              history: requestHistory(base),
              systemPrompt: sysPrompt,
              temperature: temp,
              maxTokens: maxTok,
              provider: selectedProvider,
            }),
          ),
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
              const event = parseStreamEvent(api, JSON.parse(payload));
              if (!event) continue;
              if (event.tokens) timing.tokens = event.tokens;
              if (event.error) failure = { status: 'error', error: event.error };
              if (event.finishReason) timing.finishReason = event.finishReason;
              // The router announces one routing frame per attempt, so a
              // fallback overwrites the primary — last one wins is the one
              // that actually served the response.
              if (event.routing) {
                turn.routing = event.routing;
                scheduleFlush(turnSessionId, turn);
              }
              if (event.content || event.reasoning) {
                const now = performance.now();
                if (!timing.firstToken) timing.firstToken = now;
                if (event.content && !timing.contentStart) timing.contentStart = now;
              }
              if (event.content) {
                turn.content += event.content;
                scheduleFlush(turnSessionId, turn);
              }
              if (event.reasoning) {
                turn.reasoning += event.reasoning;
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
      session,
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
      <div className="flex h-full items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
      </div>
    );
  }

  if (!hasRole(state.user?.role, 'internal')) {
    return (
      <ProtectedRoute>
        <div className="flex h-full flex-col items-center justify-center gap-4 text-gray-900">
          <h1 className="text-2xl font-semibold">Access Required</h1>
          <p className="text-sm text-gray-500">Chat is only available to internal users.</p>
          <Link href="/dashboard" className="text-sm font-medium text-crimson hover:opacity-80">
            Back to Dashboard
          </Link>
        </div>
      </ProtectedRoute>
    );
  }

  const lastUserIndex = msgs.map((m) => m.role).lastIndexOf('user');

  const railLabel = 'text-[11px] font-semibold uppercase tracking-[0.14em] text-gray-400';
  const railBlock = 'border-b border-gray-100 px-4 py-4';
  const fieldLabel = 'block text-xs font-medium text-gray-600';
  const control =
    'mt-1.5 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition placeholder:text-gray-400 hover:border-gray-300 focus:border-gray-400 focus:ring-2 focus:ring-gray-900/5 disabled:opacity-50';
  const sliderLabelRow = 'mb-2 flex items-center justify-between';

  const apiBase =
    config.apiBase || (typeof window !== 'undefined' ? window.location.origin : 'https://gateway');
  const codeHistory = input.trim()
    ? [...requestHistory(msgs), { role: 'user' as const, content: input.trim() }]
    : requestHistory(msgs);
  const codeText = buildCode(sessionApi, apiBase, {
    model: modelId || 'your-model-id',
    history: codeHistory,
    systemPrompt: sysPrompt,
    temperature: temp,
    maxTokens: maxTok,
    provider: selectedProvider,
  });
  const copyCode = async () => {
    setCodeCopied(await copyToClipboard(codeText));
  };

  return (
    <ProtectedRoute>
      <div className="flex w-full flex-col text-gray-900 lg:h-[calc(100dvh-13.5rem)] lg:min-h-[36rem]">
        <div className="mb-5 flex shrink-0 flex-wrap items-end justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2">
            <button
              ref={settingsButtonRef}
              type="button"
              onClick={() => setSettingsOpen(true)}
              aria-expanded={settingsOpen}
              aria-controls="playground-settings"
              aria-label="Chat settings"
              className="-ml-1 rounded-md p-2 text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 lg:hidden"
            >
              <svg
                aria-hidden="true"
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75" />
              </svg>
            </button>
            <div className="min-w-0">
              <h1 className="text-[22px] font-semibold tracking-tight">Chat</h1>
              <p className="mt-1 text-sm text-gray-500">
                Talk to any model in your catalog. Each session keeps its own model and
                instructions.
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              ref={codeButtonRef}
              type="button"
              onClick={() => {
                setCodeCopied(false);
                setCodeOpen(true);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-600 transition hover:border-gray-300 hover:text-gray-900"
            >
              <svg
                aria-hidden="true"
                className="h-3.5 w-3.5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M8 6l-5 6 5 6M16 6l5 6-5 6" />
              </svg>
              View code
            </button>
            <span className="hidden items-center gap-1.5 rounded-full border border-gray-200 bg-white px-2.5 py-1 text-[11px] font-medium text-gray-600 sm:inline-flex">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  streaming ? 'animate-pulse bg-emerald-500' : 'bg-gray-300'
                }`}
              />
              {streaming ? 'Streaming' : 'Ready'}
            </span>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white lg:flex-row">
          {settingsOpen && (
            <div
              aria-hidden="true"
              onClick={() => setSettingsOpen(false)}
              className="fixed inset-0 z-30 bg-black/40 lg:hidden"
            />
          )}
          <aside
            id="playground-settings"
            aria-label="Chat settings"
            className={`fixed inset-y-0 left-0 z-40 flex w-[85vw] max-w-xs flex-col overflow-hidden border-r border-gray-100 bg-gray-50 shadow-2xl duration-200 motion-reduce:transition-none lg:visible lg:static lg:z-auto lg:w-[19rem] lg:max-w-none lg:shrink-0 lg:transform-none lg:bg-gray-50/50 lg:shadow-none ${
              settingsOpen
                ? 'visible translate-x-0 transition-transform'
                : 'invisible -translate-x-full transition-[transform,visibility]'
            }`}
          >
            <div className="flex h-14 shrink-0 items-center justify-between border-b border-gray-100 px-4 lg:hidden">
              <span className="text-sm font-semibold text-gray-900">Chat settings</span>
              <button
                ref={settingsCloseRef}
                type="button"
                onClick={() => {
                  setSettingsOpen(false);
                  settingsButtonRef.current?.focus();
                }}
                aria-label="Close chat settings"
                className="rounded-md p-2 text-gray-500 transition hover:bg-gray-100 hover:text-gray-900"
              >
                <svg
                  aria-hidden="true"
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={1.75}
                  strokeLinecap="round"
                >
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
            <div className="flex items-center justify-between gap-2 border-b border-gray-100 px-4 py-3">
              <span className={railLabel}>Sessions</span>
              <button
                type="button"
                onClick={() => {
                  newSession();
                  setSettingsOpen(false);
                }}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50"
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
                    d="M12 5v14M5 12h14"
                  />
                </svg>
                New
              </button>
            </div>

            <div className="max-h-44 shrink-0 overflow-y-auto p-2">
              {sessions.map((s) => {
                const active = s.id === activeSessionId;
                return (
                  <div
                    key={s.id}
                    className={`group relative rounded-lg transition ${
                      active
                        ? 'bg-gray-100 text-gray-900'
                        : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        switchSession(s.id);
                        setSettingsOpen(false);
                      }}
                      className="w-full px-2.5 py-2 text-left"
                    >
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
                          {getSessionTitle(s)}
                        </span>
                        {streamingIds.has(s.id) && (
                          <span
                            className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-500"
                            aria-label="Streaming"
                          />
                        )}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-gray-400">
                        {getMessageCount(s.messages)}
                        {s.selectedModelId ? ` · ${s.selectedModelId}` : ''}
                      </div>
                    </button>
                    {sessions.length > 1 && !streamingIds.has(s.id) && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          deleteSession(s.id);
                        }}
                        className={`absolute right-1.5 top-1.5 rounded p-1 text-gray-400 transition hover:bg-gray-200 hover:text-gray-700 ${REVEAL_ON_HOVER}`}
                        title="Delete session"
                        aria-label={`Delete session ${getSessionTitle(s)}`}
                      >
                        <svg
                          aria-hidden="true"
                          className="h-3 w-3"
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

            <div className="min-h-0 flex-1 overflow-y-auto">
              <section className={railBlock}>
                <label htmlFor="playground-model" className={railLabel}>
                  Model
                </label>
                <div className="mt-2">
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
                </div>
                {modelsError && !modelsLoading && (
                  <div className="mt-2 flex items-start justify-between gap-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] text-red-700">
                    <span className="min-w-0 break-words">
                      Couldn&apos;t load models: {modelsError}
                    </span>
                    <button
                      type="button"
                      onClick={loadModels}
                      className="shrink-0 font-medium underline-offset-2 hover:underline"
                    >
                      Retry
                    </button>
                  </div>
                )}

                <div className="mt-3">
                  <label htmlFor="playground-api" className={railLabel}>
                    API
                  </label>
                  <Select
                    id="playground-api"
                    size="lg"
                    className="mt-2 w-full"
                    value={sessionApi}
                    onValueChange={(value) => patch((s) => ({ ...s, api: value as PlaygroundApi }))}
                    disabled={activeStreaming}
                    options={API_OPTIONS}
                  />
                  <p className="mt-2 text-[11px] leading-4 text-gray-400">
                    Request format. View code shows the same call.
                  </p>
                </div>

                {modelProviders.length > 1 && (
                  <div className="mt-3">
                    <label htmlFor="playground-provider" className={railLabel}>
                      Provider
                    </label>
                    <Select
                      id="playground-provider"
                      size="lg"
                      className="mt-2 w-full"
                      value={selectedProvider ?? ''}
                      onValueChange={(value) =>
                        patch((s) => ({
                          ...s,
                          selectedProvider: value || null,
                        }))
                      }
                      disabled={activeStreaming || sessionApi !== 'chat'}
                      options={[
                        { value: '', label: 'Auto' },
                        ...modelProviders.map((p) => ({ value: p.id, label: p.name })),
                      ]}
                    />
                  </div>
                )}
              </section>

              <section className={railBlock}>
                <span className={railLabel}>Parameters</span>
                <div className="mt-3">
                  <div className={sliderLabelRow}>
                    <label htmlFor="playground-temperature" className={fieldLabel}>
                      Temperature
                    </label>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] tabular-nums text-gray-600">
                      {temp.toFixed(1)}
                    </span>
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
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-200 accent-crimson disabled:opacity-50"
                  />
                </div>

                <div className="mt-4">
                  <div className={sliderLabelRow}>
                    <label htmlFor="playground-max-tokens" className={fieldLabel}>
                      Max tokens
                    </label>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] tabular-nums text-gray-600">
                      {maxTok}
                    </span>
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
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-gray-200 accent-crimson disabled:opacity-50"
                  />
                </div>
              </section>

              <section className="px-4 py-4">
                <label htmlFor="playground-system" className={railLabel}>
                  System prompt
                </label>
                <textarea
                  id="playground-system"
                  value={sysPrompt}
                  onChange={(e) => patch((s) => ({ ...s, systemPrompt: e.target.value }))}
                  placeholder="You are a research assistant..."
                  rows={5}
                  disabled={activeStreaming}
                  className={`${control} mt-2 resize-none leading-6`}
                />
              </section>
            </div>

            <div className="border-t border-gray-100 p-3">
              <button
                type="button"
                onClick={resetSession}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-[13px] font-medium text-gray-600 transition hover:border-gray-300 hover:text-gray-900"
              >
                Clear conversation
              </button>
            </div>
          </aside>

          <section className="flex min-h-[28rem] min-w-0 flex-1 flex-col overflow-hidden bg-white lg:min-h-0">
            <section
              ref={chatRef}
              onScroll={onChatScroll}
              aria-label="Conversation"
              className="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-6"
            >
              {msgs.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="max-w-md text-center">
                    <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-gray-200 bg-gray-50">
                      <svg
                        aria-hidden="true"
                        className="h-6 w-6 text-gray-400"
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
                    <h2 className="text-lg font-semibold tracking-tight text-gray-900">
                      Start a conversation
                    </h2>
                    <p className="mx-auto mt-1.5 max-w-xs text-sm leading-6 text-gray-500">
                      {model
                        ? `You are talking to ${model.name}. Type a message below to begin.`
                        : 'Choose a model in the settings panel, then type a message below.'}
                    </p>
                    <button
                      type="button"
                      onClick={() => setSettingsOpen(true)}
                      className="mt-4 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-600 transition hover:border-gray-400 hover:text-gray-900 lg:hidden"
                    >
                      Open chat settings
                    </button>
                  </div>
                </div>
              ) : (
                <div className="mx-auto max-w-3xl space-y-7">
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
                            {!isUser && (
                              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gray-900 text-[10px] font-semibold text-white">
                                AI
                              </span>
                            )}
                            <span className="truncate text-[13px] font-medium text-gray-700">
                              {isUser ? 'You' : msg.modelName || model?.name || 'Assistant'}
                            </span>
                            {!isUser && msg.routing && (
                              <button
                                type="button"
                                onClick={() => toggleIn(setExpandedRouting, msg.id)}
                                aria-expanded={isRoutingExpanded}
                                className={`truncate rounded px-1.5 py-0.5 text-[11px] font-medium transition ${
                                  msg.routing.fallback
                                    ? 'bg-amber-50 text-amber-700'
                                    : 'bg-gray-100 text-gray-500 hover:text-gray-700'
                                }`}
                                title={routingLines(msg.routing).join('\n')}
                              >
                                {msg.routing.fallback ? '↳ ' : ''}
                                {msg.routing.endpointId}
                              </button>
                            )}
                          </div>
                          <div className="flex shrink-0 items-center gap-1.5">
                            {!isUser && msg.durationMs != null && !isStreamingThis && (
                              <span className="hidden text-[11px] tabular-nums text-gray-400 sm:inline">
                                {msg.ttftMs != null && `${msg.ttftMs}ms TTFT · `}
                                {rate != null && `${rate.toFixed(1)} tok/s · `}
                                {formatDuration(msg.durationMs)}
                              </span>
                            )}
                            {canEdit && (
                              <button
                                type="button"
                                onClick={editLast}
                                className={`rounded px-1.5 py-0.5 text-[11px] text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 ${REVEAL_ON_HOVER}`}
                              >
                                Edit
                              </button>
                            )}
                            {canRetry && (
                              <button
                                type="button"
                                onClick={regenerate}
                                disabled={!modelId}
                                className={`rounded px-1.5 py-0.5 text-[11px] transition hover:bg-gray-100 disabled:opacity-40 ${
                                  msg.status || note
                                    ? 'text-gray-700 hover:text-gray-900'
                                    : `text-gray-500 hover:text-gray-900 ${REVEAL_ON_HOVER}`
                                }`}
                              >
                                {msg.status ? 'Retry' : 'Regenerate'}
                              </button>
                            )}
                            {msg.content && !isStreamingThis && (
                              <button
                                type="button"
                                onClick={() => void handleCopy(msg.content, msg.id)}
                                className={`rounded p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 ${REVEAL_ON_HOVER}`}
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
                                    className="h-3.5 w-3.5 text-green-600"
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
                                    className="h-3.5 w-3.5 text-red-500"
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
                          <ul className="mb-2 space-y-0.5 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-[11px] leading-5 text-gray-600">
                            {routingLines(msg.routing).map((line) => (
                              <li key={line} className="break-all">
                                {line}
                              </li>
                            ))}
                          </ul>
                        )}
                        <div
                          className={
                            isUser
                              ? 'ml-auto w-fit max-w-[85%] rounded-2xl rounded-tr-md bg-gray-100 px-4 py-2.5 text-sm leading-7 text-gray-800'
                              : 'text-sm leading-7 text-gray-800'
                          }
                        >
                          {isWaiting ? (
                            <div className="flex items-center gap-2.5 text-gray-500">
                              <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
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
                                    className="flex items-center gap-1.5 text-xs text-gray-500 transition hover:text-gray-800"
                                  >
                                    <svg
                                      aria-hidden="true"
                                      className={`h-3 w-3 transition-transform ${isThinkingExpanded ? 'rotate-90' : ''}`}
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
                                    <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 text-xs leading-6 text-gray-600 whitespace-pre-wrap">
                                      {msg.reasoningContent}
                                    </div>
                                  )}
                                </div>
                              )}
                              {isUser ? (
                                <div className="whitespace-pre-wrap">{msg.content}</div>
                              ) : (
                                msg.content && <Markdown text={msg.content} />
                              )}
                              {msg.status === 'error' && (
                                <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">
                                  <p className="break-words">{`Error: ${msg.error}`}</p>
                                  <p className="mt-0.5 text-red-600/70">
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
                                <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700">
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
            </section>

            <div className="relative shrink-0 px-4 pb-4 sm:px-6">
              {showJump && msgs.length > 0 && (
                <button
                  type="button"
                  onClick={() => scrollDown('smooth', true)}
                  className="absolute bottom-full left-1/2 mb-3 -translate-x-1/2 rounded-full border border-gray-200 bg-white/95 px-3 py-1.5 text-xs font-medium text-gray-700 shadow-lg transition hover:border-gray-300 hover:text-gray-900"
                >
                  ↓ Jump to latest
                </button>
              )}
              <div className="mx-auto max-w-3xl">
                {undo && (
                  <div
                    role="status"
                    className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-700"
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
                        className="font-medium text-crimson hover:opacity-80 disabled:opacity-40"
                      >
                        Undo
                      </button>
                      <button
                        type="button"
                        onClick={() => setUndo(null)}
                        aria-label="Dismiss"
                        className="text-gray-500 hover:text-gray-800"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                )}
                <div className="rounded-[20px] border border-gray-200 bg-white p-2 shadow-sm transition focus-within:border-gray-300 focus-within:ring-2 focus-within:ring-gray-900/5">
                  <textarea
                    ref={textareaRef}
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={onKey}
                    aria-label="Message"
                    placeholder="Type a message... (Ctrl+Enter to send)"
                    rows={1}
                    className="max-h-[200px] min-h-[48px] w-full resize-none bg-transparent px-3 py-2.5 text-sm leading-6 text-gray-900 outline-none placeholder:text-gray-400"
                  />
                  <div className="flex items-center justify-between gap-3 px-1.5 pb-0.5 pt-1">
                    <div className="flex min-w-0 items-center gap-1.5 overflow-hidden">
                      <span className="max-w-[10rem] truncate rounded-md bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                        {model ? model.name : modelsLoading ? 'Loading...' : 'No model'}
                      </span>
                      {selectedProvider && (
                        <span className="hidden max-w-[8rem] truncate rounded-md bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500 sm:inline">
                          {selectedProvider}
                        </span>
                      )}
                      <span className="hidden rounded-md bg-gray-100 px-2 py-0.5 text-[11px] tabular-nums text-gray-500 sm:inline">
                        temp {temp.toFixed(1)}
                      </span>
                      <span className="hidden rounded-md bg-gray-100 px-2 py-0.5 text-[11px] tabular-nums text-gray-500 md:inline">
                        max {maxTok}
                      </span>
                      {sysPrompt.trim() && (
                        <span className="hidden rounded-md bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500 md:inline">
                          instructions
                        </span>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {activeStreaming && (
                        <button
                          type="button"
                          onClick={() => {
                            if (session) abortControllersRef.current.get(session.id)?.abort();
                          }}
                          className="rounded-full border border-gray-300 px-3 py-1.5 text-[11px] font-medium text-gray-600 transition hover:border-gray-400 hover:text-gray-900"
                        >
                          Stop
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={send}
                        disabled={activeStreaming || !input.trim() || !modelId}
                        aria-label={activeStreaming ? 'Sending...' : 'Send'}
                        className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-gray-900 text-white transition hover:bg-black disabled:opacity-30"
                      >
                        <svg
                          aria-hidden="true"
                          className="h-4 w-4"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M12 19V5m0 0l-6 6m6-6l6 6"
                          />
                        </svg>
                      </button>
                    </div>
                  </div>
                </div>
                <p className="mt-2 text-center text-[11px] text-gray-400">
                  Responses are generated by the selected model and may be inaccurate.
                </p>
              </div>
            </div>
          </section>
        </div>

        {codeOpen && (
          <div
            className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[10vh]"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target !== event.currentTarget) return;
              setCodeOpen(false);
              codeButtonRef.current?.focus();
            }}
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-label="API request code"
              className="flex max-h-[75vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl"
            >
              <div className="flex items-start justify-between gap-3 border-b border-gray-100 px-5 py-4">
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-gray-900">View code</h2>
                  <p className="mt-0.5 text-xs text-gray-500">
                    The same request against the public API, using your own key.
                  </p>
                </div>
                <button
                  ref={codeCloseRef}
                  type="button"
                  onClick={() => {
                    setCodeOpen(false);
                    codeButtonRef.current?.focus();
                  }}
                  aria-label="Close code view"
                  className="rounded p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
                >
                  <svg
                    aria-hidden="true"
                    className="h-4 w-4"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="min-h-0 flex-1 overflow-auto bg-gray-50 p-4">
                <pre className="whitespace-pre font-mono text-[12px] leading-5 text-gray-800">
                  {codeText}
                </pre>
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-5 py-3">
                <span className="min-w-0 text-[11px] leading-4 text-gray-500">
                  Start with <code className="font-mono">export HYBRID_API_KEY=...</code> from API
                  Keys.
                </span>
                <button
                  type="button"
                  onClick={copyCode}
                  className="shrink-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 transition hover:border-gray-300 hover:text-gray-900"
                >
                  {codeCopied ? 'Copied' : 'Copy'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </ProtectedRoute>
  );
}
