'use client';

import { TaskComposer } from '@/components/features/agents/TaskComposer';

// /agents index = the new-task composer (Codex-style: "New task" is the
// default landing, running jobs live in the sidebar).
export default function AgentsPage() {
  return <TaskComposer />;
}
