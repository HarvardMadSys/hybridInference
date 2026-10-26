# Admin Console in-place editing (2026-10-26)

Supersedes the **edit** rule of
[2026-10-24-admin-console-write-confirmation.md](2026-10-24-admin-console-write-confirmation.md).
Its **action**, **create** and **read-exempt** rules, its bypass review and its inventory of API calls
are unchanged. This record covers what was changed and verified on this date. It is not a design spec.

## The rule now

| Rule | Applies to | Behaviour |
|---|---|---|
| **edit** | Changing saved values | **Field.** Editable in place, with no Edit step. It shows the draft, or the saved value when there is none. A secret field is never prefilled, and an empty secret means unchanged. Changes are local drafts, and no write is made from `onChange` or `onBlur`. **Save bar.** Appears with the first change and goes away when the change is discarded or typed back by hand. A value typed back to what is saved is no draft at all, so a later refresh of the saved value shows through instead of being masked by a stale copy. It shows the count of unsaved changes, **Discard** (zero writes) and **Save changes**. The review lists each change `old → new`. **Back to editing** keeps the drafts, and only **Confirm save** writes. The busy and failure behaviour of 2026-10-24 is unchanged. **Invalid draft.** Counts as unsaved, so the bar stays up. Settings, Batch Concurrency and Usage Insights hold Save back with the reason (`blocked`). Users, weights, limits, Queue offload and RouteWise parameters show the reason on Save, as before. **Locks.** A lock that used to wait for an open edit now waits for an unsaved change or an open confirmation. An unchanged field locks nothing. |

Entities opened explicitly keep their **Edit `<name>`** button, because the editor has to be opened:
- the provider definition and route modals;
- the capabilities modal;
- each announcement in Site updates.

They open straight into editable fields.

## Shared building blocks

- **`EditBar` `inPlace`.** The bar renders nothing until there is a change or an error, fades in, and its
  cancel reads **Discard** (aria `Discard changes to <target>`). A busy confirmation alone does not bring
  it up, since one dialog often serves the screen's other actions too. Without `inPlace` the bar keeps
  its earlier behaviour for the editors above.
- **`EditButton`.** Removed; nothing used it any more.
- **`useConfirmAction`.** A request can carry a `key`, and `savingKey` names the confirmed write that is
  running. Where several in-place bars share one confirmation, each passes `busy` only for its own key,
  so only the bar being saved reads "Saving…". This covers the routing policy, the weights and each
  route's limit in `ProviderRoutesTab`, the account settings, admin note and suspension message in
  `UserDetailPanel`, and the tiers in `ProviderKeysTab`. Test: `confirm/useConfirmAction.test.tsx`
  "names the running write by its key, and only while it runs".

## Changes by screen

### Settings tab

- **Feature Flags and Numeric Settings** (`SettingsTab.tsx`): an out-of-range number keeps the bar up
  with "Correct X before saving."
  - **Apply to existing users** is disabled, not hidden, while its own quota row has a draft, since it
    applies the saved quota. `renderExtra` takes `(setting, unsaved)`.
- **Model Visibility, Model Concurrency, Batch Concurrency:** checkboxes, selects and caps are always
  editable. A paused model shows 0 in its input; the default cap is the placeholder.
- **Usage Insights:**
  - The key field is always present and empty.
  - An emptied model holds Save with "Model name cannot be empty."
  - The fields render only after the settings load.
  - **Clear key** is still its own confirmed action. It now sits on the status line and is disabled
    while there are unsaved changes, since it would drop a typed key.

### Configuration tab

- **No one-category-at-a-time rule.** Several categories can hold drafts at once, and each saves only its
  own. A Save does not open while another review is open or a save is running.
- **Filters.** A category with unsaved changes stays visible under the search and missing-only filters.
  Its hidden-by-filter notice shows when it has changes.
- **Its own bar, not `EditBar`.** A test notification makes the Alerts category busy without saving
  anything, and `EditBar` would read "Saving…" then. The bar uses the same labels.
- **`readOnly` removed.** `ConfigEntryField` and `AlertNotificationFields` lose their `readOnly` mode,
  along with `savedValueText`, which only that mode used.

### Users

- **Account settings.** The draft holds only the changed fields, so a refetch updates untouched ones. The
  quota rules from the 2026-10-24 follow-up are unchanged.
- **Admin note, suspension message.** Always a text field. The bar shows when the trimmed value differs
  from the saved one. While the field is empty, the old read-only empty text is shown as a hint under it.

### Providers

- **Provider selector, Add key, Review key.** Held while tier changes are unsaved, with "Save or discard
  the tier changes first".
- **Switching provider** drops leftover no-op tier drafts and their error.

### Routing

- **Model selector and Create model.** Held while any of these is unsaved: a weight, a policy, a limit, a
  Queue offload or RouteWise change. Also while a modal or a confirmation is open. Tooltip: "Save or
  discard the unsaved changes first".
- **Routing policy.** The `Select` is always shown.
  - It is disabled while a weight, limit, Queue offload or RouteWise draft is unsaved. A switch unmounts
    the panel of the current policy and swaps the weight column for the limit column.
  - The disabling now covers weight and limit drafts too, not only panel drafts.
  - If the policy was chosen first, the `Select` stays changeable, and its Save is `blocked` with "Save or
    discard the other unsaved changes first".
- **Weights and limits.** One inline edit at a time is gone. Each runtime OpenRouter row has its own
  limit bar, and Save and Discard act on that row only. Weights (fixed) and limits (routewise) never
  coexist on one model.
- **Weight Reset.** Disabled while that row's weight has an unsaved change. It used to be hidden while
  editing.
- **Delete.** A confirmed **Delete** drops that route's weight and limit drafts, and deleted routes are
  left out of the change counts. Before, a weight draft on a config route deleted mid-edit was still
  counted and could be saved to the removed route.
- **Queue offload.** With no stored route, the form starts from the defaults. Filling in fields before
  choosing a route is one change, held with "Choose the offload route to save these settings".
  - With fewer than two routes the fields are disabled.
  - **Clear offload route** is visible but disabled while a change is unsaved.
- **RouteWise parameters.** **Reset** is disabled while that setting has an unsaved change. Validation
  messages show only for changed settings. The stale-response guards are unchanged.

## Tests

Every test that clicked **Edit** now edits in place. The "read-only until Edit" tests now check four
things:
- there is no Edit button;
- there is no save bar until a change;
- the bar goes again when the change is undone;
- nothing is written before **Confirm save**.

Coverage of Back, busy, failure and retry, partial saves, validation and refreshes is kept. Lock tests
are converted to their unsaved-change equivalents.

Renamed tests cited by the 2026-10-24 record:

| File | Was | Now |
|---|---|---|
| `__tests__/SettingsTab.siteConfigRefresh.test.tsx` | "shows saved values read-only, and changes nothing until the change is confirmed" | "edits saved values in place, and changes nothing until the change is confirmed" |
| `ModelVisibilitySection.test.tsx` | "shows overrides read-only until Edit" | "edits overrides in place, with no Edit step and no save bar until a change" |
| `UsageInsightsSettingsSection.test.tsx` | "shows the saved model as text and no key field until Edit" | "edits the model in place, with an empty key field and no save bar until a change" |
| `ConfigurationTab.test.tsx` | "shows saved values read-only until the category is edited" | "offers every category for editing at once, with no Edit step and no save bar until a change" |
| `ConfigurationTab.test.tsx` | "keeps the edited category, and its Cancel and Save, visible under any filter" | "keeps a category with unsaved changes visible when a search would hide it" |
| `users/UserTable.test.tsx` | "shows role, quota and concurrency as text until Edit" | "edits role, quota and concurrency in place, writing nothing until Confirm" |
| `users/UserTable.test.tsx` | "edits the message only after Edit and saves it on Confirm" | "edits the message in place and saves it only on Confirm" |
| `ProviderKeysTab.test.tsx` | "shows reservations read-only until Edit, then as editable selectors" | "edits reservations in place, with no Edit step and no save bar until a change" |
| `ProviderKeysTab.test.tsx` | "holds Add key until the tier edit is saved or cancelled" | "holds Add key until the tier changes are saved or discarded" |
| `ProviderRoutesTab.test.tsx` | "edits fixed route weights after Edit and confirms each change" | "edits fixed route weights in place and confirms each change" |
| `ProviderRoutesTab.test.tsx` | "updates a runtime OpenRouter concurrency limit after Edit and confirmation" | "updates a runtime OpenRouter concurrency limit in place after confirmation" |
| `ProviderRoutesTab.test.tsx` | "cancels a routing policy edit with zero writes" | "discards a routing policy draft with zero writes" |
| `ProviderRoutesTab.test.tsx` | "holds the routing policy while a Queue offload draft is open" | "holds the routing policy while a Queue offload draft is unsaved" |
| `ProviderRoutesTab.test.tsx` | "holds a policy switch while a parameter edit opened after it is open" | "holds a policy switch while a parameter change made after it is unsaved" |
| `OffloadRoutePanel.test.tsx` | "shows the stored route read-only and clears it only after confirmation" | "shows the stored route in place and clears it only after confirmation" |
| `RoutewiseSettingsPanel.test.tsx` | "keeps values read-only until Edit, and Cancel writes nothing" | "edits values in place with no save bar until a change, and Discard writes nothing" |

New tests:
- **Settings:** a change undone by hand hides the bar (flags, numbers, caps, the Usage Insights model);
  "holds Clear key back while there are unsaved changes"; "holds Apply back while its quota has an
  unsaved change".
- **Configuration:** "holds drafts in several categories at once, and saves only the one asked"; "saves
  changes a filter hides with the rest of their category"; "shows no save bar while the saved webhook is
  tested: a test changes nothing".
- **Users:** "shows the save bar only while the trimmed note differs, and Discard restores it"; "says
  what an emptied message means, and clears it on Confirm".
- **Provider keys:** "brings up no tier save bar while a key action is in flight".
- **Routing:** "holds the routing policy while a fixed weight is unsaved, and the other way round";
  "holds a weight Reset while that weight has an unsaved change"; "saves and discards each row's
  concurrency limit on its own", where only the row being saved reads "Saving…"; "fills in a new route
  first, and saves it once a route is chosen"; "holds Clear offload route while a change is unsaved";
  "holds Reset while that setting has an unsaved change"; and, for each panel, "reports only unsaved
  changes or an open confirmation to the parent".

Review follow-up: in `UserDetailPanel`, `RuntimeSettingsSection` and the routing weights and limits, a field typed back to its saved value used to keep its draft. The bar went away, but a refreshed saved value stayed masked and could come back as a change. Those drafts are now dropped. The tests are:
- `users/UserTable.test.tsx`: "lets a refresh show through a field typed back to its saved value" and "lets a refresh show through a note typed back to its saved text";
- `ProviderRoutesTab.test.tsx`: "lets a refreshed limit show through a draft typed back to the saved one".

All three fail without the fix. Runtime settings reload only on mount, so no refresh path reaches a reverted draft there today; the same fix applies anyway.

## Verification

Run from `apps/frontend`:

| Command | Result |
|---|---|
| `npm run format:check` | clean |
| `npm run lint` | clean |
| `npm run type-check` | clean |
| `npm run i18n:check` | clean |
| `npx vitest run` | 147 files, 1,902 tests passed |

Not done: browser acceptance against a running deployment.
