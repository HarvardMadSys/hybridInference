import React from 'react';
import clsx from 'clsx';

interface InputFieldProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: string;
  error?: string;
}

export const InputField = React.forwardRef<HTMLInputElement, InputFieldProps>(
  ({ label, hint, error, className, ...props }, ref) => (
    <label className="block">
      <span className="text-sm font-medium text-gray-700">{label}</span>
      <input
        ref={ref}
        className={clsx(
          'mt-1.5 w-full rounded-lg border bg-white px-4 py-2.5 text-sm shadow-sm transition-all duration-200',
          'placeholder:text-gray-400',
          'hover:border-gray-400',
          error
            ? 'border-red-300 focus:border-red-500 focus:ring-2 focus:ring-red-500/20'
            : 'border-gray-300 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20',
          className,
        )}
        {...props}
      />
      {hint && !error && <span className="mt-1.5 block text-xs text-gray-500">{hint}</span>}
      {error && <span className="mt-1.5 block text-xs text-red-600">{error}</span>}
    </label>
  ),
);

InputField.displayName = 'InputField';
