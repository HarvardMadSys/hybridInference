import React from 'react';
import clsx from 'clsx';

/**
 * The console's raised card. Its padding narrows on a phone; the signup
 * consent step's sticky bar bleeds to the same edges, so change both together.
 */
export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div
      className={clsx(
        'rounded-xl bg-white p-6 shadow-lg ring-1 ring-gray-200/50 sm:p-8',
        className,
      )}
    >
      {children}
    </div>
  );
}
