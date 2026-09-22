import type { Config } from 'tailwindcss';

export default {
  content: ['./src/**/*.{ts,tsx}'],
  // Sponsor sizing arrives in /site-config at runtime. The branding schema
  // restricts class_name to this finite vocabulary; safelisting the same set
  // ensures a white-label build contains every allowed utility and variant.
  safelist: [
    'h-8',
    'h-10',
    'h-12',
    'h-14',
    'h-16',
    'sm:h-8',
    'sm:h-10',
    'sm:h-12',
    'sm:h-14',
    'sm:h-16',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          primary: '#111827',
          accent: '#3B82F6',
          bgLight: '#F9FAFB',
          bgDark: '#0B1220',
        },
        // The accent ramp is a CSS variable so the active distribution's
        // branding document can re-point it without a second set of
        // components. The fallback channels are the neutral crimson, and the
        // space-separated form is what lets opacity modifiers
        // (`bg-crimson/10`) keep working.
        crimson: {
          DEFAULT: 'rgb(var(--brand-accent, 165 28 48) / <alpha-value>)',
          dark: 'rgb(var(--brand-accent-dark, 139 23 41) / <alpha-value>)',
          light: 'rgb(var(--brand-accent-light, 200 50 74) / <alpha-value>)',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        serif: ['var(--font-serif)', 'Georgia', 'Cambria', 'Times New Roman', 'serif'],
      },
      borderRadius: {
        lg: '0.5rem',
      },
      boxShadow: {
        subtle: '0 1px 2px rgba(0,0,0,0.06)',
        card: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
      },
    },
  },
  plugins: [],
} satisfies Config;
