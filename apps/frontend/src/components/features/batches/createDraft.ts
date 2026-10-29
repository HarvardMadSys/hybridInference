import type {
  BatchOverrides,
  BatchSourceFile,
  ConsoleTarget,
  PreflightReport,
} from '@/lib/api/batches';

/**
 * What the create page has been told so far. Saved per account in this
 * browser (never the file's content), so a reload or a sign-in again returns
 * to it; another account never sees it.
 */
export interface CreateDraft {
  source: BatchSourceFile | null;
  /** The type picked because the file does not state one (only then). */
  typeChoice: ConsoleTarget | null;
  name: string;
  nameEdited: boolean;
  description: string;
  modelMode: 'file' | 'override';
  model: string;
  temperatureMode: 'keep' | 'set';
  temperature: string;
  maxTokensMode: 'keep' | 'set';
  maxTokens: string;
  copiedFrom: string | null;
  /** The source of the batch being copied, to say when a re-upload is the same file. */
  originalSha256: string | null;
}

export function emptyDraft(): CreateDraft {
  return {
    source: null,
    typeChoice: null,
    name: '',
    nameEdited: false,
    description: '',
    modelMode: 'file',
    model: '',
    temperatureMode: 'keep',
    temperature: '0',
    maxTokensMode: 'keep',
    maxTokens: '1024',
    copiedFrom: null,
    originalSha256: null,
  };
}

/** The type the file states, or the one picked when it states none. */
export function draftTarget(
  draft: CreateDraft,
  report: PreflightReport | null,
): ConsoleTarget | null {
  return report?.detected.type ?? (report?.detected.needs_type_choice ? draft.typeChoice : null);
}

/** The endpoint to send with a preflight: only a choice the file needs. */
export function choiceEndpoint(draft: CreateDraft, report: PreflightReport | null): string | null {
  if (!report?.detected.needs_type_choice || !draft.typeChoice) return null;
  return draft.typeChoice === 'chat' ? '/v1/chat/completions' : '/v1/embeddings';
}

/** The overrides the draft sets: a model always may be; text parameters only for Chat. */
export function draftOverrides(draft: CreateDraft, target: ConsoleTarget | null): BatchOverrides {
  const overrides: BatchOverrides = {};
  if (draft.modelMode === 'override' && draft.model) overrides.model = draft.model;
  if (target === 'chat') {
    if (draft.temperatureMode === 'set' && draft.temperature.trim() !== '') {
      overrides.temperature = Number(draft.temperature);
    }
    if (draft.maxTokensMode === 'set' && draft.maxTokens.trim() !== '') {
      overrides.max_tokens = Number(draft.maxTokens);
    }
  }
  return overrides;
}

/** `<file>-<model>-<YYYYMMDD-HHmm>`, at most 128 characters. */
export function defaultName(
  filename: string | null,
  model: string | null,
  now = new Date(),
): string {
  const base = (filename ?? 'batch').replace(/\.jsonl?$/i, '');
  const pad = (n: number) => String(n).padStart(2, '0');
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}`;
  return [base, model, stamp].filter(Boolean).join('-').slice(0, 128);
}

const DRAFT_PREFIX = 'batch-draft:';

export function loadDraft(userId: string): CreateDraft | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_PREFIX + userId);
    if (!raw) return null;
    const value = JSON.parse(raw) as CreateDraft & { userId?: string };
    if (value.userId !== userId) return null;
    return { ...emptyDraft(), ...value };
  } catch {
    return null;
  }
}

export function saveDraft(userId: string, draft: CreateDraft): void {
  try {
    window.localStorage.setItem(DRAFT_PREFIX + userId, JSON.stringify({ ...draft, userId }));
  } catch {
    // Storage full or disabled: the draft simply is not kept.
  }
}

export function clearDraft(userId: string): void {
  try {
    window.localStorage.removeItem(DRAFT_PREFIX + userId);
  } catch {
    // Nothing to clear.
  }
}
