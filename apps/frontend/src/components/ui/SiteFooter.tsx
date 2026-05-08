import { config } from '@/config/env';
import { BuildInfo } from '@/components/ui/BuildInfo';

export function SiteFooter(): JSX.Element {
  return (
    <footer className="mx-auto w-full max-w-5xl px-6 py-6 text-center text-sm text-gray-400">
      <div className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
        <span>© {config.appName}</span>
        <span aria-hidden="true">·</span>
        <a
          href="https://madsys.seas.harvard.edu"
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Harvard SEAS
        </a>
        <span aria-hidden="true">·</span>
        <a
          href="https://doc.freeinference.org/"
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          Docs
        </a>
        <span aria-hidden="true">·</span>
        <a href="/terms" className="hover:text-crimson">
          Terms
        </a>
        <span aria-hidden="true">·</span>
        <a
          href="https://github.com/HarvardMadSys/hybridInference"
          className="hover:text-crimson"
          target="_blank"
          rel="noopener noreferrer"
        >
          GitHub
        </a>
        <span aria-hidden="true">·</span>
        <BuildInfo />
      </div>
    </footer>
  );
}
