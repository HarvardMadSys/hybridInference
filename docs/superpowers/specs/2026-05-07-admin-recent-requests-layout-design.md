# Admin Recent Requests Layout Design

## Goal

Improve the admin dashboard Recent Requests table detail view so request inspection is easier, with the full request ID shown in expanded details and noisy network internals removed.

## Current State

The admin Recent Requests tab lives in `apps/frontend/src/app/dashboard/admin/page.tsx`. It renders a dense table and expands rows inline. The expanded row currently truncates long request IDs, includes a `Network details` disclosure with peer IP, IP source, and X-Forwarded-For, and mixes identity, user, performance, network, content, and error fields in one grid.

## Approved UX Direction

Keep the main table row optimized for scanning. Do not add the request ID to the main row. Show the full request ID only in the expanded detail panel.

The expanded detail panel should be reorganized into a clearer but more compact hierarchy:

1. Request ID stays on its own block so the full value remains readable.
2. Identity metadata collapses into one inline summary row: model, provider, status, timestamp, stream, and cost.
3. Performance metadata collapses into one inline summary row: latency, TTFT, and decode throughput.
4. Token metadata collapses into one inline summary row, using grouped pairs where possible: prompt/completion, reasoning/total, and cached read/cache write.
5. User/session metadata collapses into one inline summary row: user name, user email, user ID, and session ID. Keep user IP only if it still fits the row without harming scanability; otherwise place it at the end of that row or omit it from the compact treatment and keep it on a short trailing line.
6. User agent remains its own full-width line so long values do not crowd other metadata.
7. Content: prompt, reasoning, response, using the existing lazy-loaded request-content API and this exact order.
8. Error: request error if present.

Compactness should come primarily from reduced padding, fewer bordered pills, and inline rows for short fields, not from hiding data or truncating the full request ID.

## Removed UI

Remove the expanded row `Network details` disclosure entirely. The UI should no longer display peer IP, IP source, or X-Forwarded-For in Recent Requests details.

## Data Flow

No backend or API contract changes are required. The list still uses `listRecentRequests`, and expanded content still uses `getRecentRequestContent`. The existing `reqContentCache` lazy-load behavior should remain intact.

## Files

Modify production UI code under `apps/frontend/src/app/dashboard/admin/`, including `page.tsx` and the extracted detail panel component. Add a focused test under `apps/frontend/src/app/dashboard/admin/__tests__/`. The existing API type definitions in `apps/frontend/src/lib/api/admin.ts` remain unchanged because the frontend still receives the same fields, even though some network fields are no longer rendered.

## Testing

Run frontend formatting/lint/type-check/test commands from `apps/frontend` where practical:

- `npm run format:check`
- `npm run lint`
- `npm run type-check`
- `npm run test`

If dependency installation or environment constraints prevent running all commands, record which commands were attempted and their output.
