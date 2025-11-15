import '../styles/globals.css';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';

export const metadata = {
  title: config.appName,
  description: 'Free inference service platform',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body
        className="flex min-h-screen flex-col bg-gray-50 text-black antialiased"
        suppressHydrationWarning
      >
        <ErrorBoundary>
          <Providers>
            <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
              <div className="text-xl font-bold tracking-tight">{config.appName}</div>
            </header>
            <main className="mx-auto flex w-full max-w-5xl flex-1 items-center px-6 py-12">
              {children}
            </main>
            <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
              © {config.appName}
            </footer>
          </Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
