'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { JobDetail } from '@/components/features/agents/JobDetail';
import { useAgentJob } from '@/components/features/agents/useAgentJobs';

export default function AgentJobPage() {
  const params = useParams<{ jobId: string }>();
  const jobId = params?.jobId ?? '';
  const { job, loading, error } = useAgentJob(jobId);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-gray-500">Loading job…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900">Could not load this job</h1>
        <p className="text-sm text-gray-500">{error}</p>
        <Link href="/agents" className="text-sm font-medium text-crimson hover:underline">
          Back to Agents
        </Link>
      </div>
    );
  }

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

  // Keyed on the id: JobDetail holds per-attempt state, and without a remount
  // navigating between jobs kept the previous job's selected attempt.
  return <JobDetail key={job.id} job={job} />;
}
