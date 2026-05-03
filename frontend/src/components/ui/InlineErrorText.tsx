export function InlineErrorText({ message, size = 'sm' }: { message: string; size?: 'sm' | 'xs' }) {
  const textSize = size === 'xs' ? 'text-[11px]' : 'text-xs';
  return (
    <p className={`mt-0.5 max-w-[280px] truncate text-red-500 ${textSize}`} title={message}>
      {message}
    </p>
  );
}
