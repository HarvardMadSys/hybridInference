import type { Config } from 'tailwindcss';

export default {
  content: ['./src/**/*.{ts,tsx}'],
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
        crimson: {
          DEFAULT: '#A51C30',
          dark: '#8B1729',
          light: '#C8324A',
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
