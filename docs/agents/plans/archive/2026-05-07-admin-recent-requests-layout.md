# Admin Recent Requests Compact Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admin Recent Requests expanded metadata area substantially more compact by replacing the current pill-heavy layout with dense inline summary rows while preserving the full request ID, separate cached read and cache write values, and the existing prompt/reasoning/response content order.

**Architecture:** Keep the existing admin page, API calls, and lazy-loaded content flow. Limit implementation to the extracted `AdminRecentRequestDetailPanel` component and its focused test, replacing most bordered `DetailPill` grids with inline metadata rows and leaving only long-form fields such as request ID and user agent on their own lines.

**Tech Stack:** Next.js, React 18, TypeScript, Tailwind CSS, Vitest, Testing Library.

---

### Task 1: Update the Detail Panel Test for Compact Rows

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`

- [ ] **Step 1: Write the failing test**

Replace the current assertions that look for individual token and performance pills with assertions that describe the approved compact inline-row output.

```tsx
// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AdminRecentRequestItem } from '@/lib/api/admin';
import { AdminRecentRequestDetailPanel } from '../AdminRecentRequestDetailPanel';

function makeAdminRequest(overrides: Partial<AdminRecentRequestItem> = {}): AdminRecentRequestItem {
  return {
    request_id: 'req_1234567890abcdefghijklmnopFULLID',
    user_id: 'user_abc123456789',
    user_name: 'Ada Admin',
    user_email: 'ada@example.com',
    user_ip: '203.0.113.10',
    peer_ip: '10.0.0.12',
    ip_source: 'x-forwarded-for',
    x_forwarded_for: '198.51.100.9, 10.0.0.12',
    user_agent: 'Claude-Code/1.0 long user agent value',
    session_id: 'sess_123',
    request_surface: 'openai_chat_completions',
    model_id: 'claude-sonnet',
    provider: 'anthropic',
    timestamp: '2026-05-06T12:00:00.000Z',
    status_code: 200,
    latency_ms: 2200,
    ttft_ms: 700,
    decode_throughput_tps: 42.25,
    stream: true,
    prompt_tokens: 1200,
    completion_tokens: 301,
    reasoning_tokens: 64,
    cache_read_tokens: 80,
    cache_write_tokens: 20,
    total_tokens: 1665,
    cost_usd: 0.0234,
    error: null,
    ...overrides,
  };
}

describe('AdminRecentRequestDetailPanel', () => {
  it('renders compact inline metadata rows without internal network fields', () => {
    render(
      <AdminRecentRequestDetailPanel
        req={makeAdminRequest()}
        content={{
          prompt: 'Prompt body',
          reasoning_content: 'Reasoning body',
          response: 'Response body',
          loading: false,
        }}
      />,
    );

    expect(screen.getByText('req_1234567890abcdefghijklmnopFULLID')).toBeInTheDocument();
    expect(screen.getByText(/claude-sonnet/i)).toBeInTheDocument();
    expect(screen.getByText(/anthropic/i)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.0234/)).toBeInTheDocument();
    expect(screen.getByText(/2\.2s/)).toBeInTheDocument();
    expect(screen.getByText(/700ms/)).toBeInTheDocument();
    expect(screen.getByText(/42\.3 tok\/s/)).toBeInTheDocument();
    expect(screen.getByText(/1,200 \/ 301/)).toBeInTheDocument();
    expect(screen.getByText(/64 \/ 1,665/)).toBeInTheDocument();
    expect(screen.getByText(/80 \/ 20/)).toBeInTheDocument();
    expect(screen.getByText(/Ada Admin/)).toBeInTheDocument();
    expect(screen.getByText(/ada@example.com/)).toBeInTheDocument();
    expect(screen.getByText(/sess_123/)).toBeInTheDocument();
    expect(screen.getByText('Claude-Code/1.0 long user agent value')).toBeInTheDocument();

    expect(screen.queryByText('Network details')).not.toBeInTheDocument();
    expect(screen.queryByText('Peer IP:')).not.toBeInTheDocument();
    expect(screen.queryByText('IP source:')).not.toBeInTheDocument();
    expect(screen.queryByText('X-Forwarded-For:')).not.toBeInTheDocument();
    expect(screen.queryByText('10.0.0.12')).not.toBeInTheDocument();
    expect(screen.queryByText('x-forwarded-for')).not.toBeInTheDocument();
    expect(screen.queryByText('198.51.100.9, 10.0.0.12')).not.toBeInTheDocument();

    const prompt = screen.getByText('Prompt');
    const reasoning = screen.getByText('Reasoning');
    const response = screen.getByText('Response');
    expect(
      prompt.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      reasoning.compareDocumentPosition(response) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
Expected: FAIL because the component still renders bordered pill grids and no compact inline summary strings such as `1,200 / 301`, `64 / 1,665`, or `80 / 20`.

### Task 2: Replace Pill Grids with Compact Inline Rows

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/AdminRecentRequestDetailPanel.tsx`
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`

- [ ] **Step 1: Add a reusable inline row renderer**

In `apps/frontend/src/app/dashboard/admin/AdminRecentRequestDetailPanel.tsx`, add a small helper near `DetailPill` and the formatters so the compact rows share one presentation pattern.

```tsx
function InlineMetaRow({
  items,
  mono = false,
}: {
  items: Array<string | null | undefined>;
  mono?: boolean;
}) {
  const visibleItems = items.filter((item): item is string => Boolean(item && item.trim()));
  if (visibleItems.length === 0) return null;

  return (
    <div
      className={[
        'flex flex-wrap gap-x-3 gap-y-1 border-t border-gray-100 py-1.5 text-[11px] text-gray-600',
        mono ? 'font-mono text-[11px] text-gray-700' : '',
      ].join(' ')}
    >
      {visibleItems.map((item) => (
        <span key={item}>{item}</span>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Add small string helpers for grouped compact values**

Still in `AdminRecentRequestDetailPanel.tsx`, add helpers that produce the grouped token strings required by the approved layout.

```tsx
function pairLabel(label: string, value: string): string {
  return `${label} ${value}`;
}

function pairValue(
  left: number | null | undefined,
  right: number | null | undefined,
  leftLabel: string,
  rightLabel: string,
): string | null {
  if (left == null && right == null) return null;
  return `${leftLabel} ${formatTokens(left)} / ${rightLabel} ${formatTokens(right)}`;
}
```

Use them to assemble the exact compact groupings from the spec:

```tsx
const identityItems = [
  req.model_id,
  req.provider,
  req.status_code != null ? String(req.status_code) : '—',
  relTime(req.timestamp),
  req.stream != null ? (req.stream ? 'stream' : 'non-stream') : null,
  formatCost(req.cost_usd),
];

const performanceItems = [
  pairLabel('lat', formatLatency(req.latency_ms)),
  pairLabel('ttft', formatLatency(req.ttft_ms)),
  req.decode_throughput_tps != null
    ? pairLabel('decode', `${req.decode_throughput_tps.toFixed(1)} tok/s`)
    : null,
];

const tokenItems = [
  pairValue(req.prompt_tokens, req.completion_tokens, 'in/out', ''),
  pairValue(req.reasoning_tokens, req.total_tokens, 'reason/total', ''),
  pairValue(req.cache_read_tokens, req.cache_write_tokens, 'cache r/w', ''),
].map((item) => item?.replace(' /  ', ' / '));

const userItems = [req.user_name, req.user_email, req.user_id, req.session_id, req.user_ip];
```

- [ ] **Step 3: Replace the bordered grids with the compact inline layout**

Replace the current block starting at the top-level wrapper and ending before the content area with this structure:

```tsx
return (
  <div className="rounded-lg border border-gray-200 bg-white p-3 shadow-sm">
    <div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">Request ID</div>
    <div className="mt-0.5 break-all font-mono text-[11px] text-gray-800">{req.request_id}</div>

    <InlineMetaRow items={identityItems} />
    <InlineMetaRow items={performanceItems} mono />
    <InlineMetaRow items={tokenItems} mono />
    <InlineMetaRow items={userItems} />

    <div className="border-t border-gray-100 pt-1.5 text-[11px] text-gray-500 truncate">
      {req.user_agent || '—'}
    </div>

    <div className="mt-3 grid grid-cols-1 gap-2 text-[11px]">
      {!content || content.loading ? (
        <div className="text-gray-400">Loading prompt and response…</div>
      ) : content.error ? (
        <div className="text-red-600">Failed to load content: {content.error}</div>
      ) : (
        <>
          <FoldedText label="Prompt" value={content.prompt} />
          {content.reasoning_content && (
            <FoldedText label="Reasoning" value={content.reasoning_content} />
          )}
          <FoldedText label="Response" value={content.response} />
        </>
      )}
      {req.error && <div className="mt-1 text-red-600">Error: {req.error}</div>}
    </div>
  </div>
);
```

Do not render `DetailPill` in the metadata area after this change. Keep `FoldedText`, error rendering, and the request content load states unchanged.

- [ ] **Step 4: Remove now-unused `DetailPill` code if the component no longer references it**

Delete the old helper if it is unused after the layout rewrite.

```tsx
function DetailPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5">
      <div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">{label}</div>
      <div className="mt-0.5 break-words font-mono text-[12px] text-gray-700">{value}</div>
    </div>
  );
}
```

- [ ] **Step 5: Run the focused test to verify it passes**

Run: `npm run test -- src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
Expected: PASS.

### Task 3: Verify the Compact Layout Across Frontend Quality Gates

**Files:**
- Modify: no source changes unless verification finds issues
- Test: frontend scripts from `apps/frontend`

- [ ] **Step 1: Run format check**

Run: `npm run format:check`
Expected: PASS or a concrete list of files that need formatting.

- [ ] **Step 2: Run lint**

Run: `npm run lint`
Expected: PASS.

- [ ] **Step 3: Run type check**

Run: `npm run type-check`
Expected: PASS.

- [ ] **Step 4: Run frontend tests**

Run: `npm run test`
Expected: PASS.

## Self-Review

Spec coverage: Task 1 and Task 2 implement the approved compact inline-row detail layout, preserve the full request ID, keep cached read and cache write values separate, retain prompt/reasoning/response order, and continue to omit internal network details. Task 3 covers the required frontend verification commands.

Placeholder scan: no TBD/TODO placeholders remain. Every task includes exact files, concrete assertions or code, and explicit commands.

Type consistency: `AdminRecentRequestItem`, `AdminRecentRequestDetailPanel`, and `AdminRecentRequestContentState` match the existing extracted component and test structure in `apps/frontend/src/app/dashboard/admin/`.
