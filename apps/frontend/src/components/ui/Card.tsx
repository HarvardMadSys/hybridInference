import React from 'react';
import clsx from 'clsx';

export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div className={clsx('rounded-xl bg-white p-8 shadow-lg ring-1 ring-gray-200/50', className)}>
      {children}
    </div>
  );
}
