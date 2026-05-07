# Admin Recent Requests Layout Design

## Goal

Improve the admin dashboard Recent Requests table detail view so request inspection is easier, with the full request ID shown in expanded details and noisy network internals removed.

## Current State

The admin Recent Requests tab lives in `apps/frontend/src/app/dashboard/admin/page.tsx`. It renders a dense table and expands rows inline. The expanded row currently truncates long request IDs, includes a `Network details` disclosure with peer IP, IP source, and X-Forwarded-For, and mixes identity, user, performance, network, content, and error fields in one grid.

## Approved UX Direction

Keep the main table row optimized for scanning. Do not add the request ID to the main row. Show the full request ID only in the expanded detail panel.

The expanded detail panel should be reorganized into a clearer hierarchy:

1. First line: request identity, including full request ID, model, provider, status, and timestamp.
2. Second line: performance and token information, including latency, TTFT, decode throughput, stream, prompt tokens, completion tokens, reasoning tokens, cached tokens, total tokens, and cost.
3. User/session section: user name, user email, user ID, session ID, and user IP.
4. User agent: its own full-width line so long values do not crowd other fields.
5. Content: prompt, reasoning, response, using the existing lazy-loaded request-content API and this exact order.
6. Error: request error if present.

## Removed UI

Remove the expanded row `Network details` disclosure entirely. The UI should no longer display peer IP, IP source, or X-Forwarded-For in Recent Requests details.

## Data Flow

No backend or API contract changes are required. The list still uses `listRecentRequests`, and expanded content still uses `getRecentRequestContent`. The existing `reqContentCache` lazy-load behavior should remain intact.

## Files

Modify `apps/frontend/src/app/dashboard/admin/page.tsx` only for the implementation. The existing API type definitions in `apps/frontend/src/lib/api/admin.ts` remain unchanged because the frontend still receives the same fields, even though some network fields are no longer rendered.

## Testing

Run frontend formatting/lint/type-check/test commands from `apps/frontend` where practical:

- `npm run format:check`
- `npm run lint`
- `npm run type-check`
- `npm run test`

If dependency installation or environment constraints prevent running all commands, record which commands were attempted and their output.
