'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

import { Button } from '@/components/ui/Button';
import { useAuth } from '@/components/providers';
import { useT, type Translate } from '@/components/providers/useT';
import {
  BatchConsoleError,
  getCreateContext,
  getMyBatch,
  preflightSource,
  uploadSourceFile,
  type BatchCreateContext,
  type BatchSourceFile,
  type ConsoleSubmission,
  type ConsoleTarget,
  type PreflightReport,
} from '@/lib/api/batches';
import { useBatchCapabilities } from '@/lib/hooks/queries/useBatchCapabilities';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { roleLabel } from '@/lib/utils/roleLabel';

import { formatTime } from './batchText';
import {
  choiceEndpoint,
  clearDraft,
  defaultName,
  draftOverrides,
  draftTarget,
  emptyDraft,
  loadDraft,
  saveDraft,
  type CreateDraft,
} from './createDraft';
import { ConfigureStep, ReviewStep, Stepper, UploadStep } from './createSteps';
import { SourceFilesPanel } from './SourceFilesPanel';
import { useSubmission, type SubmissionState } from './useSubmission';

/** What a refusal means for the page, in words the user can act on. */
function refusalText(error: BatchConsoleError, t: Translate): string {
  switch (error.code) {
    case 'credential_changed':
      return t(
        'batches.create.refused.credential_changed',
        'Your API key changed after you confirmed (it was regenerated, revoked or replaced). Check the submission again.',
      );
    case 'no_worker_credential':
      return t(
        'batches.create.refused.no_key',
        'A batch runs under your active API key, and you have none. Create one on the dashboard, then submit again.',
      );
    case 'model_resolution_changed':
      return t(
        'batches.create.refused.model_changed',
        'The chosen model now resolves to a different model than the one you confirmed. Check the submission again.',
      );
    case 'source_unavailable':
      return t(
        'batches.create.refused.source',
        'The source file was deleted or expired. Upload it again; your settings are kept.',
      );
    case 'quota_exceeded':
      return t(
        'batches.create.refused.quota',
        'Your daily quota is spent. Submit after it renews (00:00 UTC) or once it is raised; uploading and checking stay free.',
      );
    default:
      return error.message;
  }
}

/** The configuration a check answers for: the source, the type chosen, the overrides. */
function configKey(draft: CreateDraft, identified: PreflightReport | null): string {
  const target = draftTarget(draft, identified);
  return JSON.stringify([
    draft.source?.id ?? null,
    identified?.source.id ?? null,
    choiceEndpoint(draft, identified),
    target,
    draftOverrides(draft, target),
  ]);
}

function sourceOf(report: PreflightReport, previous: BatchSourceFile | null): BatchSourceFile {
  return {
    id: report.source.id,
    bytes: report.source.bytes,
    created_at: previous?.created_at ?? 0,
    expires_at: report.source.expires_at ?? previous?.expires_at ?? null,
    filename: report.source.filename,
    sha256: report.source.sha256,
    line_count: report.source.lines,
  };
}

/**
 * Create a Chat or Embeddings batch from a JSONL file: upload, configure, check and confirm.
 *
 * With `copyFrom`, the page opens on that batch's source and settings
 * (nothing runs until the user submits). A pending submission of this account
 * -- from a lost response, a reload or another tab -- is recovered first, with
 * its own key; see `useSubmission`.
 */
export function CreateBatchView({ copyFrom }: { copyFrom: string | null }) {
  const t = useT();
  const router = useRouter();
  const { state: auth } = useAuth();
  const userId = auth.user?.id ?? null;
  const { capabilities } = useBatchCapabilities();
  const [context, setContext] = useState<BatchCreateContext | null>(null);
  const [draft, setDraft] = useState<CreateDraft>(emptyDraft);
  const [ready, setReady] = useState(false);
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [identifiedState, setIdentified] = useState<PreflightReport | null>(null);
  // An identification counts only for the file now chosen: a report of the
  // previous file never stands in while the new one is (or failed to be) read.
  const identified =
    identifiedState && identifiedState.source.id === draft.source?.id ? identifiedState : null;
  // The validated report, with the exact configuration it was checked under.
  const [checkedState, setCheckedState] = useState<{ key: string; report: PreflightReport } | null>(
    null,
  );
  const [busy, setBusy] = useState<'upload' | 'identify' | 'validate' | null>(null);
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [blockedCopy, setBlockedCopy] = useState<string | null>(null);
  const [showFiles, setShowFiles] = useState(false);
  const [filesKey, setFilesKey] = useState(0);
  const upload = useRef<AbortController | null>(null);
  const forUser = useRef<string | null>(null);
  // Generations: an answer counts only if no newer upload, identification or
  // check started since -- a slow answer for a file or settings the user has
  // already left never changes the page, let alone what is submitted.
  const uploadSeq = useRef(0);
  const identifySeq = useRef(0);
  const checkSeq = useRef(0);
  const latest = useRef<{ draft: CreateDraft; identified: PreflightReport | null }>({
    draft: emptyDraft(),
    identified: null,
  });
  const submission = useSubmission(userId, context?.limits.submission_replay_seconds);

  const update = useCallback((patch: Partial<CreateDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  }, []);

  const refreshContext = useCallback(async () => {
    const owner = userId;
    try {
      const loaded = await getCreateContext();
      if (owner === forUser.current && loaded.user_id === owner) setContext(loaded);
    } catch (err) {
      setNotice(getErrorMessage(err));
    }
  }, [userId]);

  const identify = useCallback(
    async (source: BatchSourceFile, typeChoice: ConsoleTarget | null = null, owner = userId) => {
      const seq = ++identifySeq.current;
      checkSeq.current += 1;
      setCheckedState(null);
      setBusy('identify');
      const current = () => owner === forUser.current && seq === identifySeq.current;
      try {
        const report = await preflightSource({
          source_file_id: source.id,
          phase: 'identify',
          endpoint:
            typeChoice === null
              ? null
              : typeChoice === 'chat'
                ? '/v1/chat/completions'
                : '/v1/embeddings',
        });
        if (!current()) return null;
        setIdentified(report);
        setDraft((draft) => ({ ...draft, source: sourceOf(report, source) }));
        return report;
      } catch (err) {
        if (!current()) return null;
        if (err instanceof BatchConsoleError && err.code === 'source_unavailable') {
          setDraft((current) => ({ ...current, source: null }));
          setIdentified(null);
          setStep(1);
          setNotice(refusalText(err, t));
        } else {
          setNotice(getErrorMessage(err));
        }
        return null;
      } finally {
        if (current()) setBusy(null);
      }
    },
    [t, userId],
  );

  /** A new source or type: everything in flight for the previous one is dropped. */
  const newSelection = useCallback(() => {
    setIdentified(null);
    upload.current?.abort();
    upload.current = null;
    uploadSeq.current += 1;
    identifySeq.current += 1;
    checkSeq.current += 1;
    setCheckedState(null);
    setProgress(null);
    setBusy(null);
  }, []);

  // Load this account's context and draft (or the batch being copied); forget
  // everything of another account first.
  useEffect(() => {
    forUser.current = userId;
    newSelection();
    setContext(null);
    setDraft(emptyDraft());
    setIdentified(null);
    setStep(1);
    setNotice(null);
    setBlockedCopy(null);
    setReady(false);
    if (!userId) return;
    const owner = userId;
    void (async () => {
      await refreshContext();
      let start: CreateDraft = loadDraft(owner) ?? emptyDraft();
      if (copyFrom) {
        try {
          const original = await getMyBatch(copyFrom);
          if (owner !== forUser.current) return;
          if (original.copy.state === 'unavailable') {
            setBlockedCopy(
              original.copy.reason === 'inline_input'
                ? t(
                    'batches.detail.copy_inline',
                    'This batch was submitted as direct requests, so there is no input file to reuse. Copying it is not available yet.',
                  )
                : t(
                    'batches.detail.copy_target',
                    'Batches of this type cannot be created in the console yet; submit them through /v1/batches. You can still manage this one and download its results.',
                  ),
            );
            setReady(true);
            return;
          }
          const overrides = original.creation?.overrides ?? {};
          start = {
            ...emptyDraft(),
            name: `${original.name || original.id}-${t('batches.create.copy_suffix', 'copy')}`.slice(
              0,
              128,
            ),
            nameEdited: true,
            description: original.description ?? '',
            modelMode: overrides.model ? 'override' : 'file',
            model: overrides.model ?? '',
            temperatureMode: overrides.temperature !== undefined ? 'set' : 'keep',
            temperature: overrides.temperature !== undefined ? String(overrides.temperature) : '0',
            maxTokensMode: overrides.max_tokens !== undefined ? 'set' : 'keep',
            maxTokens: overrides.max_tokens !== undefined ? String(overrides.max_tokens) : '1024',
            copiedFrom: original.id,
            originalSha256: original.source?.sha256 ?? original.creation?.source_sha256 ?? null,
            source:
              original.copy.state === 'ready' && original.source?.id
                ? {
                    id: original.source.id,
                    bytes: original.source.bytes ?? 0,
                    created_at: 0,
                    expires_at: original.source.expires_at ?? null,
                    filename: original.source.filename ?? null,
                    sha256: original.source.sha256 ?? null,
                    line_count: null,
                  }
                : null,
          };
          if (original.copy.state === 'reupload') {
            setNotice(
              t(
                'batches.create.copy_reupload',
                'The original source file is no longer stored. Upload it again; the settings of the original batch are kept.',
              ),
            );
          }
        } catch (err) {
          if (owner !== forUser.current) return;
          setNotice(getErrorMessage(err));
        }
      }
      if (owner !== forUser.current) return;
      setDraft(start);
      setReady(true);
      if (start.source) {
        const report = await identify(start.source, start.typeChoice, owner);
        if (report && copyFrom && draftTarget(start, report)) setStep(2);
      }
    })();
    // Re-run only for another account or another batch to copy.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, copyFrom]);

  // Leaving the page (or the account) abandons an upload still running.
  useEffect(
    () => () => {
      upload.current?.abort();
      uploadSeq.current += 1;
      identifySeq.current += 1;
      checkSeq.current += 1;
    },
    [userId],
  );

  useEffect(() => {
    if (userId && ready && userId === forUser.current) saveDraft(userId, draft);
  }, [draft, ready, userId]);

  // What a settled submission means for the page.
  useEffect(() => {
    const state: SubmissionState = submission.state;
    if (state.phase === 'created') {
      if (userId) clearDraft(userId);
      router.push(`/dashboard/batches/${encodeURIComponent(state.batch.id)}`);
    } else if (state.phase === 'refused') {
      const error = state.error;
      if (error.code === 'source_unavailable') {
        setDraft((current) => ({ ...current, source: null }));
        setIdentified(null);
        setCheckedState(null);
        setStep(1);
      } else if (error.code === 'invalid_file' && error.report) {
        const { draft: now, identified: report } = latest.current;
        setCheckedState({ key: configKey(now, report), report: error.report });
        setStep(3);
      } else if (
        error.code === 'credential_changed' ||
        error.code === 'no_worker_credential' ||
        error.code === 'model_resolution_changed'
      ) {
        void refreshContext();
        setCheckedState(null);
        setStep(error.code === 'no_worker_credential' ? 3 : 2);
      }
      setNotice(refusalText(error, t));
      submission.reset();
    } else if (state.phase === 'deleted') {
      setNotice(
        t(
          'batches.create.deleted',
          'That submission created a batch which has since been deleted. Nothing was created again; submit once more if you want a new batch.',
        ),
      );
      submission.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submission.state]);

  const target = draftTarget(draft, identified);
  latest.current = { draft, identified };
  const currentKey = configKey(draft, identified);
  // A report checked under other settings is no report for these.
  const checked = checkedState?.key === currentKey ? checkedState.report : null;
  const models = useMemo(
    () => (target && context ? context.models[target] : []),
    [context, target],
  );

  async function onFile(file: File) {
    setNotice(null);
    if (!/\.jsonl$/i.test(file.name)) {
      setNotice(t('batches.create.not_jsonl', 'Choose a .jsonl file.'));
      return;
    }
    if (context && file.size > context.limits.max_file_bytes) {
      setNotice(t('batches.create.too_large', 'The file is larger than this gateway accepts.'));
      return;
    }
    const owner = userId;
    if (!owner) return;
    newSelection();
    const seq = uploadSeq.current;
    const controller = new AbortController();
    upload.current = controller;
    setBusy('upload');
    setProgress({ loaded: 0, total: file.size });
    try {
      const uploaded = await uploadSourceFile(file, {
        expectedUserId: owner,
        signal: controller.signal,
        onProgress: setProgress,
      });
      if (owner !== forUser.current || seq !== uploadSeq.current) return;
      upload.current = null;
      setProgress(null);
      setBusy(null);
      setFilesKey((key) => key + 1);
      setDraft((current) => ({
        ...current,
        source: uploaded,
        typeChoice: null,
        name: current.nameEdited ? current.name : '',
      }));
      await identify(uploaded, null, owner);
      void refreshContext();
    } catch (err) {
      if (owner !== forUser.current || seq !== uploadSeq.current) return;
      upload.current = null;
      setProgress(null);
      setBusy(null);
      if ((err as Error)?.name === 'AbortError') {
        setNotice(t('batches.create.upload_cancelled', 'Upload cancelled.'));
      } else {
        setNotice(getErrorMessage(err));
      }
    }
  }

  function goConfigure() {
    // Only the current file's identification (see `identified`).
    if (!identified || identified.source.id !== draft.source?.id) return;
    const fileModel = identified.detected.models[0]?.canonical ?? null;
    setDraft((current) => {
      const usable =
        identified.detected.models.length > 0 &&
        identified.detected.model_unset_rows === 0 &&
        identified.detected.models.every((model) => model.available) &&
        new Set(identified.detected.models.map((model) => model.canonical)).size === 1;
      return {
        ...current,
        modelMode: current.modelMode === 'override' || !usable ? 'override' : 'file',
        name:
          current.nameEdited || current.name
            ? current.name
            : defaultName(
                current.source?.filename ?? null,
                current.modelMode === 'override' ? current.model : fileModel,
              ),
      };
    });
    setStep(2);
  }

  async function check() {
    if (!draft.source || !target) return;
    setBusy('validate');
    setNotice(null);
    const owner = userId;
    const seq = ++checkSeq.current;
    const key = currentKey;
    const current = () =>
      owner === forUser.current &&
      seq === checkSeq.current &&
      key === configKey(latest.current.draft, latest.current.identified);
    try {
      const report = await preflightSource({
        source_file_id: draft.source.id,
        phase: 'validate',
        endpoint: choiceEndpoint(draft, identified),
        overrides: draftOverrides(draft, target),
      });
      if (!current()) return;
      setCheckedState({ key, report });
      setStep(3);
      void refreshContext();
    } catch (err) {
      if (!current()) return;
      if (err instanceof BatchConsoleError && err.code === 'source_unavailable') {
        setDraft((current) => ({ ...current, source: null }));
        setIdentified(null);
        setStep(1);
        setNotice(refusalText(err, t));
      } else {
        setNotice(getErrorMessage(err));
      }
    } finally {
      if (seq === checkSeq.current) setBusy(null);
    }
  }

  function submit() {
    if (!checked?.valid || !checked.model_resolution || !context?.credential || !target) return;
    // Only what was checked: the report's source is the draft's, by construction.
    if (checked.source.id !== draft.source?.id) return;
    const body: ConsoleSubmission = {
      source_file_id: checked.source.id,
      source_sha256: checked.source.sha256 ?? '',
      endpoint: checked.detected.endpoint ?? '',
      overrides: draftOverrides(draft, target),
      confirmed_model: checked.model_resolution,
      credential: { key_id: context.credential.key_id, revision: context.credential.revision },
      name: draft.name.trim(),
      description: draft.description.trim(),
      copied_from: draft.copiedFrom,
    };
    void submission.submit(body);
  }

  const phase = submission.state.phase;
  const submitting = phase === 'sending' || phase === 'confirming';
  const mayCreate = capabilities?.role_may_submit !== false && context?.role_may_submit !== false;
  const sameAsOriginal =
    draft.originalSha256 && draft.source?.sha256
      ? draft.originalSha256 === draft.source.sha256
      : null;

  let blocked: React.ReactNode = null;
  if (context && !context.submission_enabled) {
    blocked = t(
      'batches.create.blocked.auth',
      'Submitting from the console needs user authentication on this gateway. Use /v1/batches.',
    );
  } else if (context && !context.credential) {
    blocked = (
      <>
        {t(
          'batches.create.blocked.no_key',
          'A batch runs under your active API key, and you have none. Uploading and checking work without one.',
        )}{' '}
        <Link href="/dashboard" className="font-medium underline">
          {t('batches.create.blocked.create_key', 'Create an API key')}
        </Link>
      </>
    );
  } else if (phase === 'stale' || phase === 'unsupported') {
    blocked = t(
      'batches.create.blocked.pending',
      'Decide about the earlier submission above before submitting another.',
    );
  }

  if (!userId || !ready) {
    return <p className="text-sm text-gray-500">{t('batches.loading', 'Loading batches…')}</p>;
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href="/dashboard/batches" className="text-sm text-gray-500 hover:text-gray-900">
          ← {t('batches.detail.back', 'Batches')}
        </Link>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-gray-900">
          {draft.copiedFrom
            ? t('batches.create.title_copy', 'Copy as a new batch')
            : t('batches.create.title', 'Create a batch')}
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          {t(
            'batches.create.subtitle',
            'Text generation or embeddings from a JSONL file: upload it, confirm the model and settings, then check and submit.',
          )}
        </p>
      </div>

      {!mayCreate && (
        <div
          role="note"
          className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-amber-200"
        >
          {fill(
            t(
              'batches.create.cannot_submit',
              'Your plan cannot create batches: that needs the {role} plan or higher. Your existing batches stay manageable on the Batches page.',
            ),
            { role: roleLabel(context?.min_role ?? capabilities?.min_role ?? 'internal', t) },
          )}
        </div>
      )}

      <SubmissionBanner
        state={submission.state}
        onSubmitAsNew={() => {
          if (
            window.confirm(
              t(
                'batches.create.stale.confirm',
                'Submit the same content again as a new batch? If the earlier submission did create a batch that has since ended and been removed, this creates (and bills) another one.',
              ),
            )
          ) {
            if (submission.state.phase === 'stale') {
              void submission.submitAsNew(submission.state.intent.key);
            }
          }
        }}
        onDiscard={() => {
          if (submission.state.phase === 'stale') {
            void submission.discard(submission.state.intent.key);
          }
        }}
      />

      {notice && (
        <p
          role="alert"
          className="rounded-lg bg-red-50 p-3 text-sm text-red-800 ring-1 ring-red-200"
        >
          {notice}
        </p>
      )}
      {blockedCopy ? (
        <p role="note" className="text-sm text-gray-700">
          {blockedCopy}
        </p>
      ) : mayCreate ? (
        <>
          <Stepper step={step} />
          {step === 1 && (
            <UploadStep
              context={context}
              draft={draft}
              identified={identified}
              progress={progress}
              checking={busy === 'identify'}
              sameAsOriginal={sameAsOriginal}
              onFile={(file) => void onFile(file)}
              onCancelUpload={() => upload.current?.abort()}
              onChooseType={(value) => {
                update({ typeChoice: value });
              }}
              onNext={goConfigure}
            >
              <Button variant="secondary" onClick={() => setShowFiles((shown) => !shown)}>
                {showFiles
                  ? t('batches.create.files_hide', 'Hide uploaded files')
                  : t('batches.create.files_show', 'Uploaded files…')}
              </Button>
            </UploadStep>
          )}
          {step === 1 && showFiles && (
            <SourceFilesPanel
              refreshKey={filesKey}
              onPick={(file) => {
                newSelection();
                setIdentified(null);
                setDraft((current) => ({ ...current, source: file, typeChoice: null }));
                void identify(file);
              }}
              onDeleted={(fileId) => {
                void refreshContext();
                if (draft.source?.id === fileId) {
                  newSelection();
                  setDraft((current) => ({ ...current, source: null }));
                  setIdentified(null);
                }
              }}
            />
          )}
          {step === 2 && identified && target && (
            <ConfigureStep
              draft={draft}
              update={update}
              identified={identified}
              target={target}
              models={models}
              checking={busy === 'validate'}
              onBack={() => {
                // A check still running is for a page the user has left.
                checkSeq.current += 1;
                setBusy(null);
                setStep(1);
              }}
              onCheck={() => void check()}
            />
          )}
          {step === 3 && checked && target && (
            <ReviewStep
              report={checked}
              context={context}
              draft={draft}
              target={target}
              submitting={submitting}
              blocked={blocked}
              onBack={() => setStep(2)}
              onSubmit={submit}
            />
          )}
        </>
      ) : null}
    </div>
  );
}

function SubmissionBanner({
  state,
  onSubmitAsNew,
  onDiscard,
}: {
  state: SubmissionState;
  onSubmitAsNew: () => void;
  onDiscard: () => void;
}) {
  const t = useT();
  if (state.phase === 'sending' || state.phase === 'confirming') {
    return (
      <div
        role="status"
        className="rounded-lg bg-blue-50 p-4 text-sm text-blue-900 ring-1 ring-blue-200"
      >
        {state.phase === 'sending'
          ? t(
              'batches.create.pending.sending',
              'Submitting… The file is checked once more before the batch is created.',
            )
          : t(
              'batches.create.pending.confirming',
              'Confirming the result of your submission. It is sent again with the same key, so it cannot create a second batch.',
            )}
      </div>
    );
  }
  if (state.phase === 'stale') {
    return (
      <div
        role="alert"
        className="space-y-3 rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-amber-200"
      >
        <p>
          {fill(
            t(
              'batches.create.stale.text',
              'A submission from {when} ("{name}") never got an answer, and it is too old to send again safely: the gateway no longer guarantees to recognise it. No batch from it can be found now; one may have been created, finished and removed since.',
            ),
            {
              when: formatTime(Math.floor(state.intent.createdAt / 1000)),
              name: state.intent.body.name,
            },
          )}
        </p>
        <p>
          {t(
            'batches.create.stale.choice',
            'Check your batch list first. Submitting again makes a new, separate submission, which can create and bill a second batch.',
          )}
        </p>
        <div className="flex flex-wrap gap-2">
          <Link
            href="/dashboard/batches"
            className="inline-flex items-center rounded-lg bg-white px-3 py-1.5 font-medium ring-1 ring-amber-300 hover:bg-amber-100"
          >
            {t('batches.create.stale.list', 'Open the batch list')}
          </Link>
          <Button size="sm" variant="secondary" onClick={onSubmitAsNew}>
            {t('batches.create.stale.new', 'Submit again as a new batch')}
          </Button>
          <Button size="sm" variant="secondary" onClick={onDiscard}>
            {t('batches.create.stale.discard', 'Forget it')}
          </Button>
        </div>
      </div>
    );
  }
  if (state.phase === 'unsupported') {
    return (
      <div
        role="alert"
        className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-amber-200"
      >
        {t(
          'batches.create.unsupported',
          'This browser cannot coordinate a submission across tabs safely (it has neither Web Locks nor IndexedDB), so it cannot submit from here. Use another browser, or /v1/batches.',
        )}
      </div>
    );
  }
  return null;
}
