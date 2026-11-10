'use client';

import { ProjectsManager } from '@/components/features/dashboard/ProjectsManager';
import { useT } from '@/components/providers/useT';

export default function ProjectsPage() {
  const t = useT();
  return (
    <>
      <h1 className="sr-only">{t('console.nav.projects', 'Projects')}</h1>
      <ProjectsManager />
    </>
  );
}
