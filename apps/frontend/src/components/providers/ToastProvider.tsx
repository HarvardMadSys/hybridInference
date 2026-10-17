'use client';

import { useSyncExternalStore } from 'react';
import toast, { ToastBar, Toaster } from 'react-hot-toast';

// Tailwind's `sm` breakpoint: below it the console lays itself out for a phone.
const WIDE_VIEWPORT = '(min-width: 640px)';

function wideViewport(): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(WIDE_VIEWPORT)
    : null;
}

function subscribeToViewport(onChange: () => void): () => void {
  const query = wideViewport();
  query?.addEventListener('change', onChange);
  return () => query?.removeEventListener('change', onChange);
}

const isWideViewport = () => wideViewport()?.matches ?? false;
// The server has no viewport. Nothing is on screen to place before hydration,
// so the guess only has to be a valid one.
const isWideOnServer = () => false;

/**
 * Toasts sit along the bottom edge, never the top.
 *
 * The header holds the account menu in its top-right corner, and toasts pinned
 * there took the clicks meant for it in the seconds after every sign-in and
 * switch. Nor can a toast be placed just below the header: it wraps onto more
 * rows on a phone, and on the public routes a distribution's own chrome may
 * draw it. Dialogs open centred, so the bottom edge stays clear of their
 * buttons too. On a desktop a toast takes the bottom-right corner; on a phone,
 * where it spans nearly the full width anyway, it is centred.
 */
export function ToastProvider() {
  const wide = useSyncExternalStore(subscribeToViewport, isWideViewport, isWideOnServer);

  return (
    <Toaster
      position={wide ? 'bottom-right' : 'bottom-center'}
      toastOptions={{
        duration: 4000,
        style: {
          background: '#fff',
          color: '#363636',
          boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)',
          border: '1px solid #e5e7eb',
          borderRadius: '0.5rem',
          padding: '12px 16px',
        },
        success: {
          iconTheme: {
            primary: '#10b981',
            secondary: '#fff',
          },
        },
        error: {
          iconTheme: {
            primary: '#ef4444',
            secondary: '#fff',
          },
        },
      }}
    >
      {(t) => (
        <ToastBar toast={t}>
          {({ icon, message }) => (
            <>
              {icon}
              {message}
              {/* Removed rather than dismissed: a dismissed toast keeps taking
                  clicks through its exit animation, and the visitor closed it to
                  reach whatever it covered. */}
              <button
                type="button"
                onClick={() => toast.remove(t.id)}
                aria-label="Dismiss notification"
                className="-mr-1 shrink-0 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
              >
                <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
                  <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
                </svg>
              </button>
            </>
          )}
        </ToastBar>
      )}
    </Toaster>
  );
}

/**
 * Take every toast off the screen at once.
 *
 * For the auth layer, whenever this tab's account changes: a toast belongs to
 * the account whose action raised it, and "Reset OPENROUTER_API_KEY." is an
 * administrator's message, not the next account's.
 *
 * react-hot-toast keeps its toasts in a module-level store, not in the
 * `<Toaster>`, so this needs no context and works from `AuthProvider`, which
 * sits above the toaster. Remounting the toaster would not clear them, for the
 * same reason. `remove`, not `dismiss`: a dismissed toast stays on screen, and
 * readable, through its exit animation.
 */
export function clearToasts(): void {
  toast.remove();
}
