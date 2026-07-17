import Image from 'next/image';
import { branding } from '@/config/branding';
import { config } from '@/config/env';

export const metadata = {
  title: `Team | ${config.appName}`,
  description: `The people building ${config.appName}.`,
};

interface TeamMember {
  name: string;
  affiliations: string[];
  badge?: string;
  image?: string;
  website?: string;
}

const members: TeamMember[] = branding.team;

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('');
}

export default function TeamPage(): JSX.Element {
  return (
    <div className="flex w-full flex-col gap-10">
      <section className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-white via-gray-50 to-red-50/30 px-6 py-14 text-center shadow-subtle">
        <p className="text-sm font-semibold uppercase tracking-widest text-crimson">Our team</p>
        <h1 className="mx-auto mt-3 max-w-2xl font-serif text-4xl font-bold tracking-tight text-gray-900 sm:text-5xl">
          The people behind <span className="text-crimson">{config.appName}</span>
        </h1>
        <p className="mx-auto mt-5 max-w-xl text-base text-gray-600 sm:text-lg">
          A small research team building free, open LLM inference at{' '}
          <a
            href={branding.orgUrl}
            className="text-crimson hover:underline"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
          .
        </p>
      </section>

      <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {members.map((member) => (
          <li
            key={member.name}
            className="group flex flex-col items-center rounded-2xl border border-gray-200 bg-white px-6 py-8 text-center shadow-sm transition duration-200 hover:-translate-y-1 hover:border-crimson/30 hover:shadow-md"
          >
            <div className="relative h-28 w-28 shrink-0 overflow-hidden rounded-full ring-4 ring-white shadow-md">
              {member.image ? (
                <Image
                  src={member.image}
                  alt={`Photo of ${member.name}`}
                  fill
                  sizes="112px"
                  className="object-cover transition duration-300 group-hover:scale-105"
                />
              ) : (
                <span
                  role="img"
                  aria-label={`Placeholder avatar for ${member.name}`}
                  className="flex h-full w-full items-center justify-center bg-gradient-to-br from-crimson/15 to-crimson/5 text-2xl font-semibold tracking-wide text-crimson"
                >
                  {initials(member.name)}
                </span>
              )}
            </div>

            <h2 className="mt-5 font-serif text-xl font-semibold tracking-tight text-gray-950">
              {member.website ? (
                <a
                  href={member.website}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="transition hover:text-crimson hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-crimson"
                >
                  {member.name}
                </a>
              ) : (
                member.name
              )}
            </h2>

            {member.badge && (
              <span className="mt-2 inline-flex items-center rounded-full bg-crimson/10 px-3 py-0.5 text-xs font-medium uppercase tracking-wide text-crimson">
                {member.badge}
              </span>
            )}

            <ul className="mt-4 space-y-1">
              {member.affiliations.map((affiliation) => (
                <li key={affiliation} className="text-sm leading-6 text-gray-600">
                  {affiliation}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}
