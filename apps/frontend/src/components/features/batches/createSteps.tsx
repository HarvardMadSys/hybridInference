'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/Select';
import { useT } from '@/components/providers/useT';
import type {
  BatchCreateContext,
  CatalogModel,
  ConsoleTarget,
  ParamDistribution,
  PreflightError,
  PreflightReport,
} from '@/lib/api/batches';
import { fill } from '@/lib/utils/interpolate';

import { consoleTypeLabel, formatBytes, formatTime } from './batchText';
import type { CreateDraft } from './createDraft';
import { errorReportFile, saveText } from './errorReport';
import { FormatExample } from './FormatExample';

export function StepCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <h2 className="text-base font-semibold text-gray-900">{title}</h2>
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

/** The three steps, with the current one marked for assistive technology. */
export function Stepper({ step }: { step: 1 | 2 | 3 }) {
  const t = useT();
  const steps = [
    t('batches.create.step.upload', 'Upload data'),
    t('batches.create.step.configure', 'Configure'),
    t('batches.create.step.review', 'Check and confirm'),
  ];
  return (
    <ol className="flex flex-wrap gap-2 text-sm" aria-label={t('batches.create.steps', 'Steps')}>
      {steps.map((label, index) => {
        const current = index + 1 === step;
        return (
          <li
            key={label}
            aria-current={current ? 'step' : undefined}
            className={`rounded-full px-3 py-1 ring-1 ring-inset ${
              current
                ? 'bg-blue-600 text-white ring-blue-600'
                : index + 1 < step
                  ? 'bg-blue-50 text-blue-700 ring-blue-200'
                  : 'bg-white text-gray-500 ring-gray-200'
            }`}
          >
            {index + 1}. {label}
          </li>
        );
      })}
    </ol>
  );
}

export function ErrorList({ report }: { report: PreflightReport }) {
  const t = useT();
  const shown = report.errors.slice(0, 200);
  return (
    <div role="alert" className="space-y-2">
      <p className="text-sm font-medium text-red-700">
        {fill(
          t(
            'batches.create.errors.count',
            '{count} problem(s): nothing can be submitted until every one is fixed.',
          ),
          { count: report.error_count.toLocaleString() },
        )}
      </p>
      <div className="flex flex-wrap gap-2">
        {(['csv', 'jsonl'] as const).map((format) => (
          <Button
            key={format}
            size="sm"
            variant="secondary"
            onClick={() => {
              const file = errorReportFile(report, format);
              saveText(
                `${report.source.filename ?? 'batch'}.errors.${format}`,
                file.content,
                file.mime,
              );
            }}
          >
            {format === 'csv'
              ? t('batches.create.errors.download_csv', 'Download problems (CSV)')
              : t('batches.create.errors.download_jsonl', 'Download problems (JSONL)')}
          </Button>
        ))}
      </div>
      <div className="max-h-80 overflow-auto rounded-lg ring-1 ring-gray-200">
        <table className="min-w-full divide-y divide-gray-200 text-left text-xs">
          <thead className="bg-gray-50 text-gray-500">
            <tr>
              <th className="px-3 py-2 font-medium">{t('batches.create.errors.line', 'Line')}</th>
              <th className="px-3 py-2 font-medium">custom_id</th>
              <th className="px-3 py-2 font-medium">{t('batches.create.errors.field', 'Field')}</th>
              <th className="px-3 py-2 font-medium">
                {t('batches.create.errors.problem', 'Problem')}
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 bg-white text-gray-700">
            {shown.map((error: PreflightError, index) => (
              <tr key={`${error.line}-${error.code}-${index}`}>
                <td className="px-3 py-1.5 align-top">{error.line ?? '—'}</td>
                <td className="max-w-[10rem] truncate px-3 py-1.5 align-top">
                  {error.custom_id ?? '—'}
                </td>
                <td className="px-3 py-1.5 align-top">
                  <code>{error.field ?? '—'}</code>
                </td>
                <td className="px-3 py-1.5 align-top">
                  <span className="mr-1 text-gray-400">{error.code}</span>
                  {error.message}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(report.errors_truncated || report.errors.length > shown.length) && (
        <p className="text-xs text-gray-500">
          {fill(
            t(
              'batches.create.errors.truncated',
              'Showing {shown} of {total}; the downloads list {listed}.',
            ),
            {
              shown: String(shown.length),
              total: report.error_count.toLocaleString(),
              listed: String(report.errors.length),
            },
          )}
        </p>
      )}
    </div>
  );
}

// -- step 1 --------------------------------------------------------------------------

export function UploadStep({
  context,
  draft,
  identified,
  progress,
  checking,
  onFile,
  onCancelUpload,
  onChooseType,
  onNext,
  sameAsOriginal,
  children,
}: {
  context: BatchCreateContext | null;
  draft: CreateDraft;
  identified: PreflightReport | null;
  progress: { loaded: number; total: number } | null;
  checking: boolean;
  onFile: (file: File) => void;
  onCancelUpload: () => void;
  onChooseType: (target: ConsoleTarget) => void;
  onNext: () => void;
  sameAsOriginal: boolean | null;
  children?: ReactNode;
}) {
  const t = useT();
  const typeGroup = useId();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const limits = context?.limits;
  const detected = identified?.detected;
  const target = detected?.type ?? (detected?.needs_type_choice ? draft.typeChoice : null);
  return (
    <StepCard title={t('batches.create.step.upload', 'Upload data')}>
      <div
        data-testid="drop-zone"
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(event) => {
          // Moving onto the zone's own text or button is not leaving it.
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files?.[0];
          if (file) onFile(file);
        }}
        className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-8 text-center text-sm transition-colors ${
          dragging ? 'border-blue-400 bg-blue-50' : 'border-gray-300'
        }`}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          className="h-8 w-8 text-gray-400"
        >
          <path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15" />
        </svg>
        <p className="mt-2 text-base font-medium text-gray-900">
          {t('batches.create.drop_title', 'Drop a .jsonl file here')}
        </p>
        <Button
          className="mt-3"
          variant={draft.source ? 'secondary' : 'primary'}
          onClick={() => input.current?.click()}
        >
          {draft.source
            ? t('batches.create.choose_another', 'Choose another file')
            : t('batches.create.choose_file', 'Choose file')}
        </Button>
        <p className="mt-3 text-xs text-gray-500">
          {t(
            'batches.create.drop_hint',
            'The file is read by the gateway, not by your browser. Nothing runs until you confirm in step 3.',
          )}
        </p>
      </div>
      <input
        ref={input}
        type="file"
        accept=".jsonl,application/jsonl,application/x-ndjson"
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onFile(file);
          event.target.value = '';
        }}
      />
      {limits && context && (
        <p className="text-xs text-gray-500" data-testid="batch-limits">
          {fill(
            t(
              'batches.create.limits',
              'Limits on this gateway: up to {file} per file, {line} per line, {requests} requests, {uploads} uploads per hour. Storage {used} of {storage} · unfinished batches {active} of {maxActive}.',
            ),
            {
              file: formatBytes(limits.max_file_bytes),
              line: formatBytes(limits.max_line_bytes),
              requests: limits.max_requests.toLocaleString(),
              uploads: String(limits.uploads_per_hour),
              used: formatBytes(context.usage.stored_bytes),
              storage: formatBytes(limits.files_max_total_bytes),
              active: String(context.usage.active_batches),
              maxActive: String(limits.max_active_batches),
            },
          )}
        </p>
      )}
      {progress && (
        <div className="space-y-1">
          <div
            role="progressbar"
            aria-label={t('batches.create.uploading', 'Uploading')}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={
              progress.total ? Math.round((progress.loaded / progress.total) * 100) : 0
            }
            className="h-2 w-full overflow-hidden rounded-full bg-gray-100"
          >
            <div
              className="h-full bg-blue-600 transition-all"
              style={{
                width: `${progress.total ? (progress.loaded / progress.total) * 100 : 5}%`,
              }}
            />
          </div>
          <div className="flex items-center justify-between text-xs text-gray-500">
            <span>
              {fill(t('batches.create.upload_progress', 'Uploading {loaded} of {total}'), {
                loaded: formatBytes(progress.loaded),
                total: formatBytes(progress.total),
              })}
            </span>
            <Button size="sm" variant="secondary" onClick={onCancelUpload}>
              {t('batches.create.cancel_upload', 'Cancel upload')}
            </Button>
          </div>
        </div>
      )}
      {checking && (
        <p role="status" className="text-sm text-gray-500">
          {t('batches.create.identifying', 'Reading the file…')}
        </p>
      )}
      {draft.source && identified && !checking && (
        <div className="space-y-3 rounded-lg bg-gray-50 p-4 text-sm" data-testid="identified">
          <p className="font-medium text-gray-900">
            {draft.source.filename ?? draft.source.id} · {formatBytes(identified.source.bytes)} ·{' '}
            {fill(t('batches.create.rows', '{count} lines'), {
              count: identified.source.lines.toLocaleString(),
            })}
            {identified.source.sha256 && (
              <span className="ml-1 font-normal text-gray-500">
                · SHA-256 <code>{identified.source.sha256.slice(0, 12)}…</code>
              </span>
            )}
          </p>
          {sameAsOriginal !== null && (
            <p className="text-xs text-gray-600">
              {sameAsOriginal
                ? t('batches.create.same_as_original', 'Same content as the original file.')
                : t(
                    'batches.create.differs_from_original',
                    'This file differs from the one the original batch used.',
                  )}
            </p>
          )}
          {detected?.type && (
            <p data-testid="recognized-type">
              {fill(t('batches.create.recognized', 'Recognized: {type} · {count} requests'), {
                type: consoleTypeLabel(detected.type, t),
                count: (detected.rows[detected.type] + detected.rows.undetermined).toLocaleString(),
              })}
            </p>
          )}
          {detected?.needs_type_choice && (
            <fieldset className="space-y-2" aria-describedby={`${typeGroup}-hint`}>
              <legend className="font-medium text-gray-900">
                {t('batches.create.choose_type', 'Which type are these requests?')}
              </legend>
              <p id={`${typeGroup}-hint`} className="text-xs text-gray-600">
                {t(
                  'batches.create.choose_type_hint',
                  'No line says it (no url, and the body has neither messages nor input). Your choice only fills in the type; every line is still checked against it in full.',
                )}
              </p>
              {(['chat', 'embeddings'] as const).map((value) => (
                <label key={value} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={typeGroup}
                    checked={draft.typeChoice === value}
                    onChange={() => onChooseType(value)}
                  />
                  {consoleTypeLabel(value, t)}
                </label>
              ))}
            </fieldset>
          )}
          {detected && (detected.models.length > 0 || detected.model_unset_rows > 0) && (
            <ul className="space-y-0.5 text-xs text-gray-600">
              {detected.models.map((model) => (
                <li key={model.name}>
                  {fill(t('batches.create.model_rows', 'Model {model}: {count} lines'), {
                    model: model.name,
                    count: model.rows.toLocaleString(),
                  })}
                  {!model.available && (
                    <span className="ml-1 text-amber-700">
                      ⚠ {t('batches.create.model_unavailable', 'not available to you')}
                    </span>
                  )}
                </li>
              ))}
              {detected.model_unset_rows > 0 && (
                <li>
                  {fill(t('batches.create.model_unset', 'No model: {count} lines'), {
                    count: detected.model_unset_rows.toLocaleString(),
                  })}
                </li>
              )}
            </ul>
          )}
          {identified.errors.length > 0 && <ErrorList report={identified} />}
          {!detected?.type && !detected?.needs_type_choice && identified.errors.length > 0 && (
            <p className="text-sm text-gray-700">
              {t(
                'batches.create.cannot_identify',
                'The file cannot be used as it is: fix the problems above and upload it again.',
              )}
            </p>
          )}
          {draft.source.expires_at && (
            <p className="text-xs text-gray-500">
              {fill(t('batches.create.source_kept', 'The file is kept until {date}.'), {
                date: formatTime(draft.source.expires_at),
              })}
            </p>
          )}
        </div>
      )}
      <FormatExample />
      <div className="flex flex-wrap justify-between gap-2">
        {children}
        <Button onClick={onNext} disabled={!target || checking || Boolean(progress)}>
          {t('batches.create.next', 'Next')}
        </Button>
      </div>
    </StepCard>
  );
}

// -- step 2 --------------------------------------------------------------------------

function Distribution({ values }: { values: ParamDistribution | undefined }) {
  const t = useT();
  if (!values) return null;
  const parts = Object.entries(values.values).map(([value, count]) => `${value}: ${count}`);
  if (values.other) parts.push(`${t('batches.create.other_values', 'other')}: ${values.other}`);
  if (values.unset) parts.push(`${t('batches.create.unset', 'not set')}: ${values.unset}`);
  return <span className="text-xs text-gray-500">{parts.join(' · ')}</span>;
}

export function ConfigureStep({
  draft,
  update,
  identified,
  target,
  models,
  checking,
  onBack,
  onCheck,
}: {
  draft: CreateDraft;
  update: (patch: Partial<CreateDraft>) => void;
  identified: PreflightReport;
  target: ConsoleTarget;
  models: CatalogModel[];
  checking: boolean;
  onBack: () => void;
  onCheck: () => void;
}) {
  const t = useT();
  const base = useId();
  const fileModels = identified.detected.models;
  const fileModelUsable =
    fileModels.length > 0 &&
    identified.detected.model_unset_rows === 0 &&
    identified.detected.models_other_rows === 0 &&
    fileModels.every((model) => model.available) &&
    new Set(fileModels.map((model) => model.canonical)).size === 1;
  const chosen = models.find((model) => model.id === draft.model);
  const effectiveModel =
    draft.modelMode === 'override'
      ? chosen
      : models.find((model) => model.id === fileModels[0]?.canonical);
  // The parameters the model declares; the gateway drops any other. An empty
  // list declares none. Only a model not chosen yet leaves this unknown.
  const declared = effectiveModel ? effectiveModel.supported_sampling_parameters : null;
  const temperatureAllowed = declared === null || declared.includes('temperature');
  const maxTokensAllowed = declared === null || declared.includes('max_tokens');
  const maxOutput = effectiveModel?.max_output_length ?? 0;
  useEffect(() => {
    // A value set for a model that took it is never sent, hidden, to one that does not.
    const patch: Partial<CreateDraft> = {};
    if (!temperatureAllowed && draft.temperatureMode === 'set') patch.temperatureMode = 'keep';
    if (!maxTokensAllowed && draft.maxTokensMode === 'set') patch.maxTokensMode = 'keep';
    if (Object.keys(patch).length) update(patch);
  }, [temperatureAllowed, maxTokensAllowed, draft.temperatureMode, draft.maxTokensMode, update]);
  const notTaken = [
    temperatureAllowed ? null : 'temperature',
    maxTokensAllowed ? null : 'max_tokens',
  ].filter(Boolean);
  const others = Object.entries(identified.other_params);
  const nameInvalid = !draft.name.trim() || draft.name.length > 128;
  const modelMissing = draft.modelMode === 'override' ? !draft.model : !fileModelUsable;
  return (
    <StepCard title={t('batches.create.step.configure', 'Configure')}>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block text-sm">
          <span className="font-medium text-gray-700">{t('batches.create.name', 'Name')} *</span>
          <input
            className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            value={draft.name}
            maxLength={128}
            required
            aria-invalid={nameInvalid}
            onChange={(event) => update({ name: event.target.value, nameEdited: true })}
          />
        </label>
        <label className="block text-sm">
          <span className="font-medium text-gray-700">
            {t('batches.create.description', 'Description (optional)')}
          </span>
          <input
            className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            value={draft.description}
            maxLength={1000}
            onChange={(event) => update({ description: event.target.value })}
          />
        </label>
      </div>
      <p className="text-sm text-gray-700" data-testid="configure-type">
        {fill(t('batches.create.recognized', 'Recognized: {type} · {count} requests'), {
          type: consoleTypeLabel(target, t),
          count: identified.source.lines.toLocaleString(),
        })}
      </p>
      <fieldset className="space-y-2 text-sm">
        <legend className="font-medium text-gray-700">{t('batches.create.model', 'Model')}</legend>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name={`${base}-model`}
            checked={draft.modelMode === 'file'}
            disabled={!fileModelUsable}
            onChange={() => update({ modelMode: 'file' })}
          />
          {fileModelUsable
            ? fill(t('batches.create.model_file', "Use the file's model ({model})"), {
                model: fileModels[0]?.canonical ?? '',
              })
            : t(
                'batches.create.model_file_unusable',
                "Use the file's model (not possible: the file names several models, none, or one you cannot use)",
              )}
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={`${base}-model`}
              checked={draft.modelMode === 'override'}
              onChange={() => update({ modelMode: 'override' })}
            />
            {t('batches.create.model_override', 'Use one model for every request:')}
          </label>
          <Select<string>
            aria-label={t('batches.create.model_pick', 'Model for every request')}
            value={draft.model}
            onValueChange={(value) => update({ model: value, modelMode: 'override' })}
            placeholder={t('batches.create.model_placeholder', 'Choose a model')}
            options={models.map((model) => ({
              value: model.id,
              label:
                model.name && model.name !== model.id ? `${model.name} (${model.id})` : model.id,
            }))}
            className="w-72"
          />
        </div>
        {models.length === 0 && (
          <p className="text-xs text-amber-700">
            {t('batches.create.no_models', 'No model of this type is available to you.')}
          </p>
        )}
      </fieldset>
      {target === 'chat' && (
        <>
          {temperatureAllowed && (
            <fieldset className="space-y-1 text-sm">
              <legend className="font-medium text-gray-700">temperature</legend>
              <div className="flex flex-wrap items-center gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={`${base}-temperature`}
                    checked={draft.temperatureMode === 'keep'}
                    onChange={() => update({ temperatureMode: 'keep' })}
                  />
                  {t('batches.create.keep', "Keep the file's values")}
                </label>
                <Distribution values={identified.params.temperature} />
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name={`${base}-temperature`}
                  checked={draft.temperatureMode === 'set'}
                  onChange={() => update({ temperatureMode: 'set' })}
                />
                {t('batches.create.set_all', 'Set for every request to')}
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  aria-label={t(
                    'batches.create.temperature_value',
                    'temperature for every request',
                  )}
                  className="w-24 rounded-lg border border-gray-300 px-2 py-1 text-sm"
                  value={draft.temperature}
                  onChange={(event) =>
                    update({ temperature: event.target.value, temperatureMode: 'set' })
                  }
                />
                <span className="text-xs text-gray-500">0–2</span>
              </label>
            </fieldset>
          )}
          {notTaken.length > 0 && (
            <p role="note" className="text-xs text-gray-600" data-testid="params-not-taken">
              {fill(
                t(
                  'batches.create.params_not_taken',
                  'This model does not take {params}, so it cannot be set for every request.',
                ),
                { params: notTaken.join(', ') },
              )}
            </p>
          )}
          {maxTokensAllowed && (
            <fieldset className="space-y-1 text-sm">
              <legend className="font-medium text-gray-700">max_tokens</legend>
              <div className="flex flex-wrap items-center gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={`${base}-max-tokens`}
                    checked={draft.maxTokensMode === 'keep'}
                    onChange={() => update({ maxTokensMode: 'keep' })}
                  />
                  {t('batches.create.keep', "Keep the file's values")}
                </label>
                <Distribution values={identified.params.max_tokens} />
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="radio"
                  name={`${base}-max-tokens`}
                  checked={draft.maxTokensMode === 'set'}
                  onChange={() => update({ maxTokensMode: 'set' })}
                />
                {t('batches.create.set_all', 'Set for every request to')}
                <input
                  type="number"
                  min={1}
                  max={maxOutput > 0 ? maxOutput : undefined}
                  step={1}
                  aria-label={t('batches.create.max_tokens_value', 'max_tokens for every request')}
                  className="w-28 rounded-lg border border-gray-300 px-2 py-1 text-sm"
                  value={draft.maxTokens}
                  onChange={(event) =>
                    update({ maxTokens: event.target.value, maxTokensMode: 'set' })
                  }
                />
                {maxOutput > 0 && (
                  <span className="text-xs text-gray-500">
                    {fill(t('batches.create.max_output', 'at most {max} for this model'), {
                      max: maxOutput.toLocaleString(),
                    })}
                  </span>
                )}
              </label>
            </fieldset>
          )}
        </>
      )}
      {others.length > 0 && (
        <p className="text-xs text-gray-600">
          {t('batches.create.other_params', 'Other parameters in the file, kept as they are:')}{' '}
          {others.map(([key, count]) => `${key} (${count})`).join(' · ')}
        </p>
      )}
      <div className="flex flex-wrap justify-between gap-2">
        <Button variant="secondary" onClick={onBack}>
          {t('batches.create.back', 'Back')}
        </Button>
        <Button onClick={onCheck} disabled={checking || nameInvalid || modelMissing}>
          {checking
            ? t('batches.create.checking', 'Checking every line…')
            : t('batches.create.check', 'Check and preview')}
        </Button>
      </div>
    </StepCard>
  );
}

// -- step 3 --------------------------------------------------------------------------

export function ReviewStep({
  report,
  context,
  draft,
  target,
  submitting,
  blocked,
  onBack,
  onSubmit,
}: {
  report: PreflightReport;
  context: BatchCreateContext | null;
  draft: CreateDraft;
  target: ConsoleTarget;
  submitting: boolean;
  /** Why submitting is not possible now, if it is not. */
  blocked: ReactNode | null;
  onBack: () => void;
  onSubmit: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState<number | null>(null);
  const resolution = report.model_resolution;
  const limits = context?.limits;
  const credential = context?.credential;
  const overrides = Object.entries({
    temperature: draft.temperatureMode === 'set' && target === 'chat' ? draft.temperature : null,
    max_tokens: draft.maxTokensMode === 'set' && target === 'chat' ? draft.maxTokens : null,
  }).filter(([, value]) => value !== null);
  return (
    <StepCard title={t('batches.create.step.review', 'Check and confirm')}>
      {!report.valid ? (
        <>
          <ErrorList report={report} />
          <div className="flex justify-start">
            <Button variant="secondary" onClick={onBack}>
              {t('batches.create.back', 'Back')}
            </Button>
          </div>
        </>
      ) : (
        <>
          <p role="status" className="text-sm text-green-700">
            ✓{' '}
            {fill(
              t(
                'batches.create.valid',
                '{count} requests are valid. Checking is free and does not guarantee that every request will succeed.',
              ),
              { count: report.request_count.toLocaleString() },
            )}
          </p>
          <dl className="divide-y divide-gray-100 text-sm" data-testid="confirmation">
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">{t('batches.create.summary.batch', 'Batch')}</dt>
              <dd className="sm:col-span-2">
                {draft.name} · {consoleTypeLabel(target, t)} ·{' '}
                {fill(t('batches.create.summary.count', '{count} requests'), {
                  count: report.request_count.toLocaleString(),
                })}
                {report.detected.source === 'choice' && (
                  <span className="block text-xs text-gray-500">
                    {t(
                      'batches.create.summary.type_choice',
                      'Type chosen by you; url filled in on every line.',
                    )}
                  </span>
                )}
                {report.detected.source === 'structure' && (
                  <span className="block text-xs text-gray-500">
                    {t(
                      'batches.create.summary.type_structure',
                      'Type recognized from the request bodies; url filled in on every line.',
                    )}
                  </span>
                )}
              </dd>
            </div>
            {resolution && (
              <div className="grid gap-1 py-2 sm:grid-cols-3">
                <dt className="text-gray-500">{t('batches.create.model', 'Model')}</dt>
                <dd className="sm:col-span-2" data-testid="confirmed-model">
                  {resolution.requested === resolution.canonical
                    ? resolution.canonical
                    : fill(
                        t(
                          'batches.create.summary.model_alias',
                          '{requested}, which is {canonical}: the batch runs on {canonical}',
                        ),
                        resolution,
                      )}
                </dd>
              </div>
            )}
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">{t('batches.create.summary.params', 'Parameters')}</dt>
              <dd className="sm:col-span-2">
                {overrides.length
                  ? overrides
                      .map(([key, value]) =>
                        fill(
                          t('batches.create.summary.override', '{key} = {value} on every request'),
                          {
                            key,
                            value: String(value),
                          },
                        ),
                      )
                      .join(' · ')
                  : t('batches.create.summary.no_overrides', "The file's values, unchanged")}
              </dd>
            </div>
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">
                {t('batches.create.summary.window', 'Execution window')}
              </dt>
              <dd className="sm:col-span-2">
                {fill(
                  t(
                    'batches.create.summary.window_text',
                    'Every request starts within {hours} hours; requests already started may finish after that. Requests not started by then expire.',
                  ),
                  { hours: String(limits?.completion_window_hours ?? 24) },
                )}
              </dd>
            </div>
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">{t('batches.create.summary.billing', 'Billing')}</dt>
              <dd className="sm:col-span-2">
                {t(
                  'batches.create.summary.billing_text',
                  'Each request is billed like the same live request; there is no batch discount and no estimate. It counts toward your daily quota, and the batch pauses when the quota is spent.',
                )}
              </dd>
            </div>
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">
                {t('batches.create.summary.credential', 'Runs under')}
              </dt>
              <dd className="sm:col-span-2">
                {credential ? (
                  <>
                    {fill(t('batches.create.summary.key', 'API key {prefix}…'), {
                      prefix: credential.key_prefix,
                    })}
                    <span className="block text-xs text-gray-500">
                      {t(
                        'batches.create.summary.key_text',
                        'Signing out or closing the browser does not stop the batch. Revoking or regenerating this key fails its unfinished requests.',
                      )}
                    </span>
                  </>
                ) : (
                  t('batches.create.summary.no_key', 'No active API key')
                )}
              </dd>
            </div>
            <div className="grid gap-1 py-2 sm:grid-cols-3">
              <dt className="text-gray-500">
                {t('batches.create.summary.retention', 'Retention')}
              </dt>
              <dd className="sm:col-span-2">
                {fill(
                  t(
                    'batches.create.summary.retention_text',
                    'Results are kept {days} days after the batch ends. The source file is kept until {date}.',
                  ),
                  {
                    days: String(limits?.result_retention_days ?? 7),
                    date: formatTime(report.source.expires_at ?? draft.source?.expires_at ?? null),
                  },
                )}
                {report.stores_copy && (
                  <span className="block text-xs text-gray-500">
                    {t(
                      'batches.create.summary.copy',
                      'The batch stores its own copy of the requests as shown below; the copy counts toward your storage and is deleted with the batch or the source file.',
                    )}
                  </span>
                )}
              </dd>
            </div>
          </dl>
          <div className="space-y-2">
            <h3 className="text-sm font-medium text-gray-900">
              {t('batches.create.preview', 'Stored requests (first lines)')}
            </h3>
            <p className="text-xs text-gray-500">
              {t(
                'batches.create.preview_hint',
                'This is what the batch stores. The gateway may still adapt a request for its provider when it runs (for example its format); that is not shown.',
              )}
            </p>
            <ul className="space-y-2">
              {report.preview.map((entry, index) => (
                <li key={entry.custom_id} className="rounded-lg bg-gray-50 p-3 text-xs">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      {fill(t('batches.create.preview_line', 'Line {line} · {custom_id}'), {
                        line: String(entry.line),
                        custom_id: entry.custom_id,
                      })}
                      {entry.filled.length > 0 && (
                        <span className="ml-2 text-gray-500">
                          {fill(t('batches.create.preview_filled', 'set here: {fields}'), {
                            fields: entry.filled.join(', '),
                          })}
                        </span>
                      )}
                      {entry.truncated && (
                        <span className="ml-2 font-medium text-amber-700">
                          {t('batches.create.preview_truncated', 'shortened for display')}
                        </span>
                      )}
                    </span>
                    <Button
                      size="sm"
                      variant="secondary"
                      aria-expanded={open === index}
                      onClick={() => setOpen(open === index ? null : index)}
                    >
                      {open === index
                        ? t('batches.create.preview_hide', 'Hide JSON')
                        : t('batches.create.preview_show', 'Show JSON')}
                    </Button>
                  </div>
                  {open === index && (
                    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-white p-2 ring-1 ring-gray-200">
                      {JSON.stringify(entry.request, null, 2)}
                    </pre>
                  )}
                </li>
              ))}
            </ul>
          </div>
          {blocked && (
            <div
              role="alert"
              className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900 ring-1 ring-amber-200"
            >
              {blocked}
            </div>
          )}
          <div className="flex flex-wrap justify-between gap-2">
            <Button variant="secondary" onClick={onBack} disabled={submitting}>
              {t('batches.create.back', 'Back')}
            </Button>
            <Button
              onClick={onSubmit}
              disabled={submitting || Boolean(blocked)}
              aria-busy={submitting || undefined}
            >
              {submitting
                ? t('batches.create.submitting', 'Submitting…')
                : t('batches.create.submit', 'Confirm and submit')}
            </Button>
          </div>
        </>
      )}
    </StepCard>
  );
}
