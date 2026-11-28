'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { ROUTING_MODELS_PATH, ROUTING_PATH } from '@/components/features/admin/routingRoutes';

/**
 * The Routing tab's pages: the routes, where a model is created and its
 * providers and weights are set, and the model catalog with the overrides
 * that apply on top of it. Each has its own address, so a link opens it.
 */
const SECTIONS = [
  { href: ROUTING_PATH, label: 'Routes' },
  { href: ROUTING_MODELS_PATH, label: 'Models' },
] as const;

/** The segmented control above every Routing page. */
export function RoutingSectionNav() {
  const pathname = usePathname() ?? '';
  return (
    <nav
      aria-label="Routing sections"
      className="mb-4 inline-flex overflow-hidden rounded-lg border border-gray-200 bg-white"
    >
      {SECTIONS.map(({ href, label }) => {
        // Routes owns only its own address; Models also owns the addresses
        // below it.
        const isActive =
          pathname === href || (href !== ROUTING_PATH && pathname.startsWith(`${href}/`));
        return (
          <Link
            key={href}
            href={href}
            prefetch={false}
            aria-current={isActive ? 'page' : undefined}
            className={`px-3.5 py-1.5 text-[13px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gray-400 ${
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
