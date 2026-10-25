'use client';

import { Select } from '@/components/ui/Select';

import type { AlertChannelChoice } from './alertNotificationDrafts';

const CHANNELS: ReadonlyArray<{ value: AlertChannelChoice; label: string }> = [
  { value: 'none', label: 'None' },
  { value: 'slack', label: 'Slack' },
  { value: 'wecom', label: 'WeCom' },
];

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
  return (
    <Select
      id={id}
      value={value}
      onValueChange={onChange}
      options={CHANNELS}
      disabled={disabled}
      aria-describedby={describedBy}
      searchable={false}
      className="w-full sm:max-w-xs"
    />
  );
}
