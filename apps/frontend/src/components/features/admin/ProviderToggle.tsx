'use client';

/**
 * The on/off switch for a provider, or one of its keys, on Admin → Providers:
 * in the registry's Routing column and on each quota card. It acts on the
 * click; switching it back undoes it.
 */
export function ProviderToggle({
  disabled,
  busy,
  onToggle,
  label,
  noun = 'Provider',
}: {
  disabled: boolean;
  busy: boolean;
  onToggle: () => void;
  label: string;
  noun?: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={busy}
      role="switch"
      aria-checked={!disabled}
      aria-label={`${disabled ? 'Enable' : 'Disable'} ${label}`}
      title={disabled ? `${noun} disabled — click to enable` : `${noun} enabled — click to disable`}
      className={`relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition disabled:opacity-50 ${
        disabled ? 'bg-gray-300' : 'bg-emerald-500'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
          disabled ? 'translate-x-0.5' : 'translate-x-[18px]'
        }`}
      />
    </button>
  );
}
