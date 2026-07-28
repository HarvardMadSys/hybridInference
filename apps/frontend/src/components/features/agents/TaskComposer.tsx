'use client';

import { useState } from 'react';

// New-task composer (the /agents index state). Runtime × model are
// first-class controls — BYOA × BYOM is the product, not an advanced option.
// The pickers are static until the Job API lands (issue #1041); the P-1
// harness verdict for the selected pair renders next to the model.
export function TaskComposer() {
  const [task, setTask] = useState('');

  return (
    <section className="mx-auto w-full max-w-2xl px-6 pb-16 pt-24">
      <h1 className="text-center text-2xl font-bold text-gray-900">What should the agent do?</h1>
      <p className="mt-1.5 text-center text-sm text-gray-500">
        Runs in an isolated sandbox. No credentials inside — the output is a draft PR.
      </p>

      <div className="mt-8 rounded-2xl bg-white shadow-sm ring-1 ring-gray-200 focus-within:ring-2 focus-within:ring-gray-300">
        <textarea
          rows={4}
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="Describe a task… e.g. Fix the SSE total-timeout regression on /v1/messages and add a unit test"
          className="w-full resize-none rounded-t-2xl border-0 bg-transparent px-5 pt-4 text-[15px] leading-relaxed placeholder:text-gray-400 focus:outline-none focus:ring-0"
        />

        <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 px-3.5 py-2.5">
          <button
            type="button"
            title="P0 runs against our own repo only"
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100"
          >
            <svg
              className="h-3.5 w-3.5 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
              />
            </svg>
            hybridInference
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100"
          >
            <svg
              className="h-3.5 w-3.5 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M7 3v12m0 0a3 3 0 1 0 3 3m-3-3a3 3 0 0 1 3 3m7-15a3 3 0 1 1-3 3m3-3v6a4 4 0 0 1-4 4H10"
              />
            </svg>
            dev
          </button>
          <span className="h-4 w-px bg-gray-200" />
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-crimson" />
            Claude Code
            <svg
              className="h-3 w-3 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="m6 9 6 6 6-6" />
            </svg>
          </button>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100"
          >
            qwen3.6-35b <span className="text-[11px] font-normal text-emerald-600">local</span>
            <svg
              className="h-3 w-3 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="m6 9 6 6 6-6" />
            </svg>
          </button>
          <span
            className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-600"
            title="P-1 harness verdict for this runtime × model pair"
          >
            <svg
              className="h-3 w-3"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2.5}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
            </svg>
            verified
          </span>
          <button
            type="button"
            disabled
            title="Skeleton — submitting arrives with the Job API (issue #1041)"
            className="ml-auto inline-flex cursor-not-allowed items-center gap-1.5 rounded-lg bg-gray-900 px-3.5 py-1.5 text-[13px] font-medium text-white opacity-60"
          >
            Run
            <svg
              className="h-3.5 w-3.5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14m0 0-6-6m6 6-6 6" />
            </svg>
          </button>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-center gap-4 text-[12px] text-gray-400">
        <span>
          Budget <span className="font-medium text-gray-600">$2.00</span>
        </span>
        <span>
          Timeout <span className="font-medium text-gray-600">30 min</span>
        </span>
        <span title="setup = Trusted (deps install) · agent = PlatformOnly (gateway + events only)">
          Network: setup Trusted · agent PlatformOnly
        </span>
      </div>
    </section>
  );
}
