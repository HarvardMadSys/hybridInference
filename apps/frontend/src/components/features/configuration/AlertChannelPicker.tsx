'use client';

import { useEffect, useRef, useState, type KeyboardEvent } from 'react';

import type { AlertChannelChoice } from './alertNotificationDrafts';

const CHANNELS: ReadonlyArray<{ value: AlertChannelChoice; label: string; description: string }> = [
  { value: 'none', label: 'None', description: 'Do not send notifications' },
  { value: 'slack', label: 'Slack', description: 'Incoming webhook' },
  { value: 'wecom', label: 'WeCom', description: 'Group webhook' },
];

function ChannelIcon({ channel }: { channel: AlertChannelChoice }) {
  return (
    <span
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-gray-100 text-gray-500"
      aria-hidden="true"
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {channel === 'none' ? (
          <>
            <circle cx="10" cy="10" r="6.5" />
            <path d="M6.5 10h7" />
          </>
        ) : channel === 'slack' ? (
          <>
            <path d="M7.5 3.5v13M12.5 3.5v13M3.5 7.5h13M3.5 12.5h13" />
          </>
        ) : (
          <>
            <path d="M12.5 11.5H6l-3 2V5.5A2 2 0 0 1 5 3.5h7.5a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2Z" />
            <path d="M7 14.5h7l3 2v-9" />
          </>
        )}
      </svg>
    </span>
  );
}

/** A select-only combobox: navigation previews an option; selection commits it. */
export function AlertChannelPicker({
  id,
  value,
  onChange,
  disabled,
  describedBy,
}: {
  id: string;
  value: AlertChannelChoice;
  onChange: (value: AlertChannelChoice) => void;
  disabled: boolean;
  describedBy?: string;
}) {
  const [open, setOpen] = useState(false);
  const selectedIndex = CHANNELS.findIndex((channel) => channel.value === value);
  const [highlighted, setHighlighted] = useState(selectedIndex);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const expanded = open && !disabled;
  const listId = `${id}-options`;

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!expanded) return;
    const dismissOutside = (event: Event) => {
      if (event.target instanceof Node && !containerRef.current?.contains(event.target)) {
        setOpen(false);
      }
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('focusin', dismissOutside);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside);
      document.removeEventListener('focusin', dismissOutside);
    };
  }, [expanded]);

  const show = (index = selectedIndex) => {
    setHighlighted(index);
    setOpen(true);
  };

  const choose = (index: number) => {
    if (disabled) return;
    const next = CHANNELS[index].value;
    setOpen(false);
    if (next !== value) onChange(next);
    triggerRef.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled || event.ctrlKey || event.metaKey || event.altKey) return;
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowUp':
        event.preventDefault();
        if (!expanded) show();
        else
          setHighlighted(
            (index) =>
              (index + (event.key === 'ArrowDown' ? 1 : CHANNELS.length - 1)) % CHANNELS.length,
          );
        break;
      case 'Home':
      case 'End':
        event.preventDefault();
        show(event.key === 'Home' ? 0 : CHANNELS.length - 1);
        break;
      case 'Enter':
      case ' ':
        event.preventDefault();
        if (expanded) choose(highlighted);
        else show();
        break;
      case 'Escape':
        if (expanded) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
        }
        break;
      case 'Tab':
        setOpen(false);
        break;
      default: {
        const index = CHANNELS.findIndex((channel) =>
          channel.label.toLowerCase().startsWith(event.key.toLowerCase()),
        );
        if (event.key.length === 1 && index >= 0) {
          event.preventDefault();
          show(index);
        }
      }
    }
  };

  return (
    <div ref={containerRef} className="relative w-full sm:max-w-xs">
      <button
        ref={triggerRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={expanded}
        aria-controls={expanded ? listId : undefined}
        aria-activedescendant={expanded ? `${listId}-${highlighted}` : undefined}
        aria-describedby={describedBy}
        disabled={disabled}
        onClick={() => (expanded ? setOpen(false) : show())}
        onKeyDown={onKeyDown}
        className="flex w-full items-center gap-3 rounded-lg border border-gray-200 bg-white px-3 py-2 text-left text-[13px] font-medium text-gray-900 shadow-sm transition hover:border-gray-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-300 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-gray-50 disabled:opacity-50"
      >
        <ChannelIcon channel={value} />
        <span className="flex-1">{CHANNELS[selectedIndex].label}</span>
        <svg
          aria-hidden="true"
          width="16"
          height="16"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          className={`text-gray-400 transition-transform ${expanded ? 'rotate-180' : ''}`}
        >
          <path d="m6 8 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {expanded ? (
        <ul
          id={listId}
          role="listbox"
          aria-label="Notification channel"
          className="absolute left-0 right-0 top-full z-30 mt-1.5 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg"
        >
          {CHANNELS.map((channel, index) => (
            <li key={channel.value} role="none">
              <button
                id={`${listId}-${index}`}
                type="button"
                role="option"
                aria-label={channel.label}
                aria-selected={value === channel.value}
                tabIndex={-1}
                disabled={disabled}
                onPointerMove={() => setHighlighted(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(index)}
                className={`flex w-full cursor-pointer items-center gap-3 rounded-lg px-2 py-2 text-left text-[13px] ${highlighted === index ? 'bg-gray-100' : 'bg-white'}`}
              >
                <ChannelIcon channel={channel.value} />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium text-gray-900">{channel.label}</span>
                  <span className="block text-[11px] text-gray-500">{channel.description}</span>
                </span>
                {value === channel.value ? (
                  <svg
                    aria-hidden="true"
                    width="16"
                    height="16"
                    viewBox="0 0 20 20"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    className="text-gray-600"
                  >
                    <path
                      d="m4.5 10 3.5 3.5 7.5-7.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
