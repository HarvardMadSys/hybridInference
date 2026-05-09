# Admin Duplicate Navbar Design

## Goal

Remove the duplicate admin dashboard navigation bar while keeping the bottom admin tab navigation intact.

## Current State

The admin dashboard shell was recently split into a shared layout and per-tab routes. `apps/frontend/src/app/dashboard/admin/layout.tsx` now renders the shared admin chrome for all admin pages: the back link to `/dashboard`, the `Admin` title and description, and the bottom tab navigation through `AdminTabNav`.

The legacy landing page at `apps/frontend/src/app/dashboard/admin/page.tsx` still renders its own copy of the same admin header and top-level tab controls. When the plain `/dashboard/admin` route is opened, both layers render, producing two admin navigation/header blocks.

## Approved UX Direction

Keep `/dashboard/admin` as a working landing page. Do not redirect it to a specific tab route.

Keep the bottom admin tab navigation that comes from `apps/frontend/src/app/dashboard/admin/layout.tsx` as the single source of truth for admin navigation. The shared layout should continue to own:

1. The back link to `/dashboard`.
2. The `Admin` heading and description.
3. The bottom tab navigation rendered by `AdminTabNav`.

Remove the duplicate admin header and duplicate tab row from `apps/frontend/src/app/dashboard/admin/page.tsx` so the landing page only renders its own content and page-specific controls.

## Implementation Approach

Use the smallest possible change:

1. Leave `apps/frontend/src/app/dashboard/admin/layout.tsx` unchanged because it already provides the correct shared admin shell for the landing page and all nested tab routes.
2. Edit `apps/frontend/src/app/dashboard/admin/page.tsx` to stop rendering:
   - the duplicate dashboard back link block,
   - the duplicate `Admin` heading and description,
   - the duplicate top-level admin tab controls.
3. Preserve page-local behavior in `page.tsx`, including refresh behavior and existing landing-page content.

This keeps the routing structure introduced by the recent admin tab decomposition intact and avoids moving ownership of shared UI back into the page component.

## Files

Modify frontend production code in:

- `apps/frontend/src/app/dashboard/admin/page.tsx`

The following file remains the owner of the admin shell and should not need behavior changes for this fix:

- `apps/frontend/src/app/dashboard/admin/layout.tsx`

## Risks And Guardrails

The main risk is accidentally removing controls that are still specific to the landing page rather than the shared shell. The edit should therefore remove only the duplicated shell markup and leave landing-page actions intact.

Do not change routing behavior, tab labels, tab destinations, or nested admin tab pages as part of this fix.

## Testing

Verify the behavior from the frontend app after the change:

1. Open `/dashboard/admin` and confirm only one admin header/nav block remains.
2. Confirm the bottom tab navigation is still visible and functional.
3. Confirm the landing page content still renders under the shared layout.

Run focused frontend checks from `apps/frontend` where practical:

- `npm run lint`
- `npm run type-check`
- `npm run test`

If the full frontend test suite is too expensive for this small change, run at least the most targeted available verification for the affected admin route and record what was executed.
