'use client';

import type { ReactNode } from 'react';
import { useT } from '@/components/providers/useT';
import { InlineErrorText } from '@/components/ui/InlineErrorText';

/** One API key as the list shows it. The caller decides what of the key a reader may see. */
export interface KeyEntry {
  id: string;
  /** `null` for a key its owner gave no name. */
  name: string | null;
  /** The masked key. Left out where a reader sees a key by its name only. */
  masked?: string;
  /** A word beside the name for a key that is not active: its status. */
  statusNote?: string;
  /** What the key spent, and its limits, in one line. */
  figures: ReactNode;
  /** A control at the end of the line, for a list a reader can act on. */
  action?: ReactNode;
}

/** One project as the list shows it, with its keys. */
export interface ProjectEntry {
  id: string;
  name: string;
  /** The account's default project. Marked, unless its name already says so. */
  isDefault?: boolean;
  /** What the project spent, and its limits, in one line. */
  figures: ReactNode;
  keys: KeyEntry[];
}

function KeyLine({ entry }: { entry: KeyEntry }) {
  const t = useT();
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-x-3 py-1 text-[12px]">
      <span className="min-w-0 truncate">
        <span className={entry.name ? 'font-medium text-gray-900' : 'italic text-gray-400'}>
          {entry.name ?? t('console.projects.unnamed_key', 'Unnamed key')}
        </span>
        {entry.masked !== undefined && (
          <>
            {' '}
            <code className="text-gray-500">{entry.masked}</code>
          </>
        )}
        {entry.statusNote !== undefined && (
          <span className="ml-1.5 text-[11px] text-gray-400">{entry.statusNote}</span>
        )}
      </span>
      <span className="tabular-nums text-gray-500">{entry.figures}</span>
      {entry.action}
    </li>
  );
}

function KeyList({ keys }: { keys: KeyEntry[] }) {
  const t = useT();
  if (keys.length === 0) {
    return (
      <p className="mt-1 pl-3 text-[12px] text-gray-400">
        {t('console.projects.no_keys', 'No keys.')}
      </p>
    );
  }
  return (
    <ul className="mt-1 divide-y divide-gray-100 border-l-2 border-gray-100 pl-3">
      {keys.map((entry) => (
        <KeyLine key={entry.id} entry={entry} />
      ))}
    </ul>
  );
}

/**
 * The projects of one account and the keys in each, as a list to read. The
 * caller reads them and writes each line of figures; keys whose project is
 * archived come last, under a heading of their own. `projects` is `null`
 * until they are read.
 */
export function ProjectsAndKeys({
  projects,
  archivedKeys = [],
  error = null,
}: {
  projects: ProjectEntry[] | null;
  archivedKeys?: KeyEntry[];
  error?: string | null;
}) {
  const t = useT();
  const archived = t('console.projects.archived', 'Archived projects');
  return (
    <div className="space-y-2 rounded-lg border border-gray-200 bg-white p-4">
      <div className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
        {t('console.projects.title', 'Projects and keys')}
      </div>
      {error && <InlineErrorText message={error} />}
      {!projects && !error && (
        <p className="text-[12px] text-gray-400">{t('console.projects.loading', 'Loading…')}</p>
      )}
      {projects?.map((project) => (
        <section key={project.id} aria-label={project.name} className="pt-1">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-[13px]">
            <span className="font-semibold text-gray-900">
              {project.name}
              {project.isDefault && project.name !== 'Default' && (
                <span className="ml-1.5 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
                  {t('console.projects.default', 'Default')}
                </span>
              )}
            </span>
            <span className="text-[12px] tabular-nums text-gray-500">{project.figures}</span>
          </div>
          <KeyList keys={project.keys} />
        </section>
      ))}
      {archivedKeys.length > 0 && (
        <section aria-label={archived} className="pt-1">
          <div className="text-[13px] font-semibold text-gray-500">{archived}</div>
          <KeyList keys={archivedKeys} />
        </section>
      )}
    </div>
  );
}
