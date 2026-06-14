import Image from 'next/image';
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
}

const members: TeamMember[] = [
  {
    name: 'Juncheng Yang',
    affiliations: ['Assistant Professor at Harvard University'],
    badge: 'Lead',
    image: 'https://junchengyang.com/img/me4.jpg',
  },
  {
    name: 'Murphy Tian',
    affiliations: [
      'Research Intern at Harvard University',
      'Undergraduate at University of Toronto',
    ],
    badge: 'Core developer',
  },
  {
    name: 'Haoran Ni',
    affiliations: ['Research Intern at Harvard University', 'Undergraduate at NJU'],
  },
];

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
    <article className="mx-auto w-full max-w-3xl rounded-2xl border border-gray-200 bg-white px-6 py-8 shadow-sm sm:px-10 sm:py-10">
      <div className="border-b border-gray-200 pb-6">
        <h1 className="text-3xl font-bold tracking-tight text-gray-950 sm:text-4xl">Team</h1>
        <p className="mt-4 text-sm leading-6 text-gray-600">
          The people building {config.appName} at Harvard SEAS.
        </p>
      </div>

      <ul className="mt-8 space-y-6">
        {members.map((member) => (
          <li
            key={member.name}
            className="flex items-start gap-4 rounded-xl border border-gray-200 bg-gray-50 px-5 py-4 sm:gap-5 sm:px-6 sm:py-5"
          >
            <div className="relative h-16 w-16 shrink-0 overflow-hidden rounded-full border border-gray-200 bg-gray-100">
              {member.image ? (
                <Image
                  src={member.image}
                  alt={`Photo of ${member.name}`}
                  fill
                  sizes="64px"
                  className="object-cover"
                />
              ) : (
                <span
                  role="img"
                  aria-label={`Placeholder avatar for ${member.name}`}
                  className="flex h-full w-full items-center justify-center text-lg font-semibold text-gray-500"
                >
                  {initials(member.name)}
                </span>
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="text-xl font-semibold tracking-tight text-gray-950">
                  {member.name}
                </h2>
                {member.badge && (
                  <span className="rounded-full bg-crimson/10 px-2.5 py-0.5 text-xs font-medium text-crimson">
                    {member.badge}
                  </span>
                )}
              </div>
              <ul className="mt-2 space-y-1">
                {member.affiliations.map((affiliation) => (
                  <li key={affiliation} className="text-sm leading-6 text-gray-700">
                    {affiliation}
                  </li>
                ))}
              </ul>
            </div>
          </li>
        ))}
      </ul>
    </article>
  );
}
