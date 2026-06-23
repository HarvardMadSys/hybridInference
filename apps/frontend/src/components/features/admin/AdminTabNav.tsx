'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const TABS = [
  { slug: 'users', label: 'Users' },
  { slug: 'requests', label: 'Recent Requests' },
  { slug: 'providers', label: 'Providers' },
  { slug: 'routing', label: 'Routing' },
  { slug: 'token-usage', label: 'Token Usage' },
  { slug: 'audit', label: 'Audit Log' },
  { slug: 'announcements', label: 'Announcements' },
  { slug: 'analytics', label: 'Analytics' },
  { slug: 'settings', label: 'Settings' },
] as const;

export function AdminTabNav() {
  const pathname = usePathname() ?? '';
  return (
    <div className="mt-6 flex flex-wrap items-center gap-1">
      {TABS.map((tab) => {
        const href = `/dashboard/admin/${tab.slug}`;
        const isActive = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link
            key={tab.slug}
            href={href}
            className={`rounded-md px-3.5 py-1.5 text-[13px] font-medium transition ${
              isActive
                ? 'bg-gray-900 text-white'
                : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900'
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </div>
  );
}
