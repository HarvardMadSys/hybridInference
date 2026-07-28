'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { JobDetail } from '@/components/features/agents/JobDetail';
import { getAgentJob } from '@/components/features/agents/mock';

export default function AgentJobPage() {
  const params = useParams<{ jobId: string }>();
  const job = getAgentJob(params?.jobId ?? '');

  if (!job) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900">Job not found</h1>
        <p className="text-sm text-gray-500">It may have been pruned, or the link is stale.</p>
        <Link href="/agents" className="text-sm font-medium text-crimson hover:underline">
          Back to Agents
        </Link>
      </div>
    );
  }

  return <JobDetail job={job} />;
}
