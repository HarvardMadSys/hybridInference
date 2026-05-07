# Admin Recent Requests Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the admin dashboard Recent Requests expanded detail view so it shows the full request ID, removes noisy network internals, and presents content in prompt/reasoning/response order.

**Architecture:** Keep the current admin page and API calls. Extract the expanded request detail panel into a small exported component in `apps/frontend/src/app/dashboard/admin/page.tsx` so it can be tested directly without rendering the whole admin page. Replace the existing inline expanded-row grid with that component.

**Tech Stack:** Next.js, React 18, TypeScript, Tailwind CSS, Vitest, Testing Library.

---

### Task 1: Add Failing Detail Panel Test

**Files:**
- Create: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
- Modify: none
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AdminRecentRequestItem } from '@/lib/api/admin';
import { AdminRecentRequestDetailPanel } from '../page';

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
  it('shows full request details without internal network fields', () => {
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
    expect(screen.queryByText('req_1234567890abcdefghijkl…')).not.toBeInTheDocument();

    const performanceLine = screen.getByLabelText('Request performance and token details');
    expect(within(performanceLine).getByText('Latency')).toBeInTheDocument();
    expect(within(performanceLine).getByText('2.2s')).toBeInTheDocument();
    expect(within(performanceLine).getByText('TTFT')).toBeInTheDocument();
    expect(within(performanceLine).getByText('700ms')).toBeInTheDocument();
    expect(within(performanceLine).getByText('Decode')).toBeInTheDocument();
    expect(within(performanceLine).getByText('42.3 tok/s')).toBeInTheDocument();

    expect(screen.getByText('User agent')).toBeInTheDocument();
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
    expect(prompt.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(reasoning.compareDocumentPosition(response) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
Expected: FAIL because `AdminRecentRequestDetailPanel` is not exported from `../page`.

### Task 2: Implement Detail Panel Component

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/page.tsx`
- Test: `apps/frontend/src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`

- [ ] **Step 1: Add component types and helpers near existing request helpers**

Add a reusable content type matching the current `reqContentCache` map value:

```tsx
type AdminRecentRequestContentState = {
  prompt: string | null;
  response: string | null;
  reasoning_content: string | null;
  loading: boolean;
  error?: string;
};
```

- [ ] **Step 2: Implement `AdminRecentRequestDetailPanel`**

Create an exported component in `page.tsx` before `export default function AdminPage()` that accepts:

```tsx
export function AdminRecentRequestDetailPanel({
  req,
  content,
}: {
  req: AdminRecentRequestItem;
  content?: AdminRecentRequestContentState;
}) {
  // render full request id, second-line performance/tokens, user/session,
  // user agent line, prompt/reasoning/response, and error
}
```

Use existing helpers: `formatLatency`, `formatTokens`, `FoldedText`, and `relTime`. Do not render `peer_ip`, `ip_source`, `x_forwarded_for`, or `Network details`.

- [ ] **Step 3: Replace inline expanded-row grid**

In the expanded request row, replace the existing `<div className="grid grid-cols-2...">...</div>` with:

```tsx
<AdminRecentRequestDetailPanel
  req={req}
  content={reqContentCache.get(req.request_id)}
/>
```

- [ ] **Step 4: Run focused test to verify it passes**

Run: `npm run test -- src/app/dashboard/admin/__tests__/AdminRecentRequestDetailPanel.test.tsx`
Expected: PASS.

### Task 3: Verify Frontend Quality Gates

**Files:**
- Modify: no source changes unless verification finds issues
- Test: frontend scripts

- [ ] **Step 1: Run format check**

Run: `npm run format:check`
Expected: PASS or report exact formatting files.

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

Spec coverage: Task 2 implements the full request ID in detail only, removes internal network fields, separates user agent, moves performance/tokens to the second detail line, and preserves lazy content loading in prompt/reasoning/response order. Task 3 verifies the frontend.

Placeholder scan: no TBD/TODO placeholders remain.

Type consistency: `AdminRecentRequestContentState` matches the existing `reqContentCache` value shape and `AdminRecentRequestItem` matches the existing API type.
