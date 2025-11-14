import React from 'react';
import clsx from 'clsx';

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'subtle';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  isLoading?: boolean;
}

const base =
  'inline-flex items-center justify-center rounded-md font-medium transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed';

const sizes: Record<ButtonSize, string> = {
  sm: 'h-9 px-3 text-sm',
  md: 'h-10 px-4 text-sm',
  lg: 'h-11 px-5 text-base',
};

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-black text-white hover:bg-gray-800 focus:ring-black shadow-sm hover:shadow-md',
  secondary:
    'bg-white text-black border border-gray-200 hover:bg-gray-50 focus:ring-gray-300 shadow-sm',
  danger: 'bg-red-600 text-white hover:bg-red-700 focus:ring-red-700 shadow-sm hover:shadow-md',
  subtle: 'bg-gray-100 text-black hover:bg-gray-200 focus:ring-gray-300',
};

export function Button({
  variant = 'primary',
  size = 'md',
  isLoading = false,
  className,
  children,
  disabled,
  ...props
}: ButtonProps): JSX.Element {
  return (
    <button
      className={clsx(base, sizes[size], variants[variant], className)}
      aria-busy={isLoading || undefined}
      disabled={disabled || isLoading}
      {...props}
    >
      {isLoading ? (
        <span className="flex items-center gap-2">
          <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-solid border-current border-r-transparent"></span>
          <span>加载中...</span>
        </span>
      ) : (
        children
      )}
    </button>
  );
}
