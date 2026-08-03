import { TaskComposer } from '@/components/features/agents/TaskComposer';

interface AgentsPageProps {
  searchParams?: Promise<{ repo?: string | string[] }>;
}

// /agents index = the new-task composer (Codex-style: "New task" is the
// default landing, running jobs live in the sidebar).
export default async function AgentsPage({ searchParams }: AgentsPageProps) {
  const repo = (await searchParams)?.repo;
  return <TaskComposer initialRepo={typeof repo === 'string' ? repo : undefined} />;
}
