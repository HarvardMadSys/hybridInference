'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { BATCHES_PATH, BATCH_REQUESTS_PATH } from '@/components/features/admin/batchRoutes';

/** The Batches tab's pages. Each has its own address, so a link opens it. */
const SECTIONS = [
  { href: BATCHES_PATH, label: 'Batches' },
  { href: BATCH_REQUESTS_PATH, label: 'Requests' },
] as const;

/** The segmented control above every Batches page. */
export function BatchesSectionNav() {
  const pathname = usePathname() ?? '';
  return (
    <nav
      aria-label="Batches sections"
      className="mb-4 inline-flex overflow-hidden rounded-lg border border-gray-200 bg-white"
    >
      {SECTIONS.map(({ href, label }) => {
        // The batch list owns only its own address; every other page also owns
        // the addresses below it.
        const isActive =
          pathname === href || (href !== BATCHES_PATH && pathname.startsWith(`${href}/`));
        return (
          <Link
            key={href}
            href={href}
            prefetch={false}
            aria-current={isActive ? 'page' : undefined}
            className={`px-3.5 py-1.5 text-[13px] font-medium transition ${
              isActive ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-50'
            }`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
