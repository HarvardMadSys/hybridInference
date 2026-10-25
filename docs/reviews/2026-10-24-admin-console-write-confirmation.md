# Admin Console write confirmation: inventory and coverage (2026-10-24)

Follow-up to PR #1629. Every Admin Console write now goes through one interaction model. This
record lists each mutation call site in `apps/frontend/src/app/dashboard/admin/` and
`apps/frontend/src/components/features/admin/`, the rule it falls under, how it is covered and which
tests check it. It is evidence of what was implemented and verified on this date, not a design spec.

## Rules applied

| Rule | Applies to | Behaviour |
|---|---|---|
| **edit** | Changing saved values | Read-only text by default; a secret reads only "Set" / "Not set". An explicit **Edit** opens the section, row or entity. Changes are local drafts, and no write is made from `onChange` or `onBlur`. **Cancel** discards the drafts with zero writes. **Save changes** shows the count of pending changes and is disabled for a no-op. The confirmation lists each changed field `old → new`. **Back to editing** keeps the drafts; only **Confirm save** writes. While busy, a second Confirm, Back, Esc and the backdrop are all blocked. On failure the meaningful drafts are kept and the error is shown. |
| **action** | Discrete actions: delete, enable/disable, reset, publish, send/test-send, snooze/resume, mute/unmute, cancel delivery, apply quota, clear logs, approve/reject, suspend/reactivate | One action-specific confirmation naming the target and the impact; no Edit step. Destructive actions use the danger tone, and existing typed confirmations are kept. |
| **create** | Add / Create forms | The form opens only from an explicit Add/Create, and a review confirmation of what will be created comes before the write. |
| **read-exempt** | Search, filter, sort, pagination, refresh, previews, verifications that store nothing, downloads, local view preferences | Direct, with no confirmation. |

### Multi-request saves

`confirm/writeEach.ts` handles saves that need one request per item:
- **The first request fails.** Nothing was written. The dialog stays open with the error, and Confirm retries safely.
- **A later request fails.** The earlier writes stand. The review closes and the edit bar shows "`<item>` could not be saved: … N changes were saved; the rest are still unsaved." Only the unsaved drafts remain, so a retry is reviewed afresh and never replays a write that succeeded.

The Configuration tab's category save uses neither `writeEach` nor `useConfirmAction`. From
`ConfigurationTab.commitSave` it sends one PATCH for the values and secret flags, then one DELETE per
staged reset. On any failure, the first request's included, the review closes and the reason is shown
beside the category's Save and on the setting it names, with how many changes were saved when some
were. What was written drops out of the drafts and the rest stays staged, so a retry is reviewed afresh
and sends only what is left.

### Shared building blocks

All are under `apps/frontend/src/components/features/admin/confirm/`:
- **`useConfirmAction.tsx`**: one dialog per screen. A `run` that resolves closes the dialog; a `run` that throws keeps it open with the error inside. It also provides the busy guard and an optional `requireText` typed confirmation. An open confirmation is never replaced: a request made while one is open, failed or not, is refused rather than queued, so a request that resolves after a read (Send Now's recipient count, the role-quota preview) cannot turn the button of another open confirmation into its own write. The administrator asks again once the open one is cancelled or confirmed. Tests: `confirm/useConfirmAction.test.tsx` ("an open confirmation is never replaced"), `BroadcastsTab.test.tsx` ("keeps an open test-send confirmation when a Send Now count arrives after it"), `__tests__/SettingsTab.roleQuota.test.tsx` ("keeps an open Remove confirmation when the quota preview arrives after it").
- **`ChangeList.tsx`**: the `old → new` rows. Long values wrap anywhere.
- **`EditBar.tsx`**: Cancel and Save changes, with the change count. An optional `blocked` reason replaces the count and keeps Save disabled while the save would drop another open edit. **`EditButton`** opens a section, row or entity for editing; an optional `title` says why it is disabled.
- **`writeEach.ts`**: the multi-request save described above.
- **`withoutSecret.ts`**: blanks a just-typed secret out of an error message, as a second guard wherever a write carries one: provider keys, provider definitions, the Usage Insights key, and secret values in a Configuration save or a new variable.

They render the existing `ConfirmDialog`, which received three changes:
- a `confirmDisabled` prop, used by the typed confirmation;
- inputs are now included in its focus loop;
- `cancelLabel`, which was added earlier in this PR.

Initial focus stays on Cancel/Back. Esc means Back, and a busy dialog cannot be closed. The height cap and inner scrolling from `ae0af9e6` are unchanged.

## Inventory

The call site names the file and the function that makes the write: the handler whose confirmation's
`run` holds it, or a function that only a confirmation reaches. Names rather than line numbers, so the
references hold as the files change. Test files sit next to their source file unless the path is given.

### Settings tab and its sections

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `updateRuntimeSetting` (bool) | `app/dashboard/admin/SettingsTab.tsx` `RuntimeSettingsSection` `save` | Feature Flags → Edit → switch → Save changes | edit | Values are read-only text (On/Off) until Edit. The confirmation shows `On → Off`; one request per changed flag, through `writeEach`. The site config is refreshed after any write, even a partial one. | `__tests__/SettingsTab.siteConfigRefresh.test.tsx`: "re-reads it after a flag changes", "shows saved values read-only, and changes nothing until the change is confirmed", "leaves it alone when the change is refused, keeping the dialog and the draft" |
| `updateRuntimeSetting` (numbers) | same `save` | Numeric Settings → Edit → input → Save changes (or Enter) | edit | The range validation is kept: an invalid draft disables Save and shows its message inline. The confirmation shows `3 → 5`. | "re-reads it after a number is saved", "refuses an out-of-range number without asking" |
| `previewRoleQuotaApply` | `SettingsTab.tsx` `onClickApply`, before its confirmation | Apply to existing users | read-exempt | Preview only (a read). | `__tests__/SettingsTab.roleQuota.test.tsx` |
| `applyRoleQuota` | `SettingsTab.tsx` `onClickApply` | Apply to existing users → confirm | action (danger) | The bespoke overlay is replaced by the shared dialog. It shows the preview's key and user counts and warns that per-key overrides are lost. | `__tests__/SettingsTab.roleQuota.test.tsx` |
| `addSignupAllowedDomain` | `SettingsTab.tsx` `onAddDomain` | Add domain → input → Review and add → Add domain | create | The input appears only after Add domain. Validation is kept. The review states the domain and its wildcard meaning; Back keeps the input. | `SettingsTab.siteConfigRefresh.test.tsx`: "adds a domain only through Add domain and a review" |
| `removeSignupAllowedDomain` | `SettingsTab.tsx` `onRemove` | Remove `<domain>` | action (danger) | The bespoke overlay is replaced by the shared dialog, which names the domain and the impact. | "removes a domain only after a confirmation naming it" |
| `updateModelVisibility` | `ModelVisibilitySection.tsx` `save` | Model Visibility → Edit → select → Save changes | edit | Previously written on `onChange`. Now a per-model `writeEach` with partial-failure reporting. | `ModelVisibilitySection.test.tsx` (13 tests: read-only, Cancel, no-op, old→new, Back, Confirm once, busy, failure+retry, partial+retry, cache refresh, and #1640's browse and reselect tests in edit mode); `__tests__/SettingsTab.modelVisibility.test.tsx` |
| `updateModelConcurrency` | `ModelConcurrencySection.tsx` `save` | Model Concurrency Limit → Edit → checkbox → Save changes | edit | Previously written on `onChange`. | `ModelConcurrencySection.test.tsx` (8 tests); `__tests__/SettingsTab.modelConcurrency.test.tsx` |
| `snoozeAlerts` | `AlertSnoozeSection.tsx` `onSnooze` | Duration button | action | The dialog names the time until which no alert is sent. | `AlertSnoozeSection.test.tsx` |
| `clearAlertSnooze` | `AlertSnoozeSection.tsx` `onResume` | Resume now | action | | `AlertSnoozeSection.test.tsx` |
| `muteAlertType` | `AlertMutesSection.tsx` `onMute` | Choose a duration in "Mute for…" | action | Choosing a duration opens the confirmation and the select resets; nothing is written on change. The error stays in the dialog. | `AlertMutesSection.test.tsx` |
| `unmuteAlertType` | `AlertMutesSection.tsx` `onUnmute` | Unmute | action | | `AlertMutesSection.test.tsx` |
| `updateUsageInsightsSettings` | `UsageInsightsSettingsSection.tsx` `onSave` | Usage Insights → Edit → model / key → Save changes | edit | The key field exists only in edit mode. The confirmation says `Set → Replace` and never shows the typed key, and an error that echoes it shows `••••` (`withoutSecret`). An empty model is refused inline. | `UsageInsightsSettingsSection.test.tsx` (new, 7 tests), including "masks the typed key in a failed save, and keeps it for the retry" |
| `updateUsageInsightsSettings({api_key:''})` | `UsageInsightsSettingsSection.tsx` `onClearKey` | Clear key | action (danger) | | "clears the key only after a confirmation" |

### Configuration tab

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `patchConfig` (category) | `components/features/admin/ConfigurationTab.tsx` `commitSave`, from the category's review | Edit category → Save changes → Confirm save | edit | Unchanged from 95d66693/a1028d97: the category flow plus staged Reset and Mark as secret. A secret value typed into the save is masked as `••••` in a refusal, beside the save and on the setting. | `ConfigurationTab.test.tsx`, including "masks a typed secret that a refused save echoes back" |
| `resetConfigKey` | `ConfigurationTab.tsx` `commitSave` | Staged Reset inside the category save | edit (staged) | Confirmed in the same list, with partial-save reporting. The save is one PATCH, then one DELETE per reset, and the backend checks each request by itself, so a pair valid only together (resetting `TRUST_X_REAL_IP` while turning `TRUST_PROXY_HEADERS` off) is refused at the PATCH. No fixed order avoids that: the reverse pair (resetting `TRUST_PROXY_HEADERS` while turning `TRUST_X_REAL_IP` off) needs the PATCH first, and two dependent resets need their own order. So the review says when a save is several requests, and a refused PATCH says which staged resets were not sent and to save the reset on its own first. | "says in the review that the resets follow the value changes as separate requests", "says the reset was not sent when the value change before it is refused", "keeps only the unwritten steps after a reset fails, and retries just those" |
| `patchConfig` (Add variable) | `ConfigurationTab.tsx` `AddVariableForm` `submit` | Add variable → Review and add → Add variable | create | **New.** A review lists the name, the value (a secret reads "Set (hidden)") and the secret flag. Back keeps the form. A secret value is masked as `••••` in an error shown in the review. | "adds a variable on Enter", "adds a custom variable, always secret…", "lets the administrator make any other variable secret", "masks the secret value of a refused new variable in its review" |
| `testAlertNotification` | `ConfigurationTab.tsx` `sendAlertTest` | Send test notification → Send test | action (test-send) | **New.** It names the channel and says whether the typed or the saved URL is used. The result is shown inline with the existing sanitized message, never as a raw error in the dialog, so a webhook cannot leak. | 8 alert tests now confirm first; "sends no test notification when its confirmation is cancelled" |

### Requests tab

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `clearErrorRequests` | `components/features/admin/RequestsTab.tsx` `handleClearErrors` | Clear last hour errors | action (danger) | `window.confirm` is replaced by the shared dialog. | `RequestsTab.test.tsx`: "deletes the last hour of errors only after a confirmation, once" |
| `exportRequests` | `lib/api/admin.ts` (GET download) | Export | read-exempt | Download only. | — |

### Users (`app/dashboard/admin/users/`)

The handlers in `index.tsx` perform the writes and now throw on failure, so the dialog keeps the error.
They are called only from confirmed `run`s in `UserTable.tsx` and `UserDetailPanel.tsx`.

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `updateUser` (role, daily quota, max concurrent, model access) | `users/index.tsx` `handlers.onUpdate` via `UserDetailPanel.tsx` `AccountSettings` `save` | Account settings → Edit → Save changes → Confirm save | edit | Read-only until Edit. `old → new` review, one PATCH, existing validation kept. **Fixed a double PATCH**: the old save called `updateUser` and then `onUpdate` again. | `UserTable.test.tsx` ("shows role, quota… until Edit", Cancel, no-op, old→new/Back/Confirm, busy, failure+retry); `UsersTabWrites.test.tsx` ("sends exactly one PATCH…") |
| `updateUser {admin_note}` / `{suspension_message}` | `handlers.onUpdate` via `UserDetailPanel.tsx` `TextSetting` `save` (note and message editors) | Edit → Save → Confirm | edit | Same pattern. | "saves a trimmed admin note…", "clears an existing note…", "edits the message only after Edit…" |
| `updateUser {status:'suspended'}` | `users/index.tsx` `handlers.onSuspend` via `UserTable.tsx` `requestSuspend` | Suspend | action (danger) | The bespoke modal is replaced. An optional message field sits in the dialog. | "suspends with the optional message…", "sends a null suspension message…" |
| `updateUser {status:'active'}` | `handlers.onUpdate` via `UserTable.tsx` `requestReactivate` | Reactivate | action | | "reactivates a suspended user…" |
| `approveUser` | `handlers.onApprove` via `UserTable.tsx` `requestApprove` | Approve | action | | "approves a pending user…", "shows a failed write inside the confirmation…" |
| `rejectUser` | `handlers.onReject` via `UserTable.tsx` `requestReject` | Reject | action (danger) | A required reason is checked on Confirm. | "rejects only with a reason" |
| `deleteUser` (soft delete) | `handlers.onDelete` via `UserTable.tsx` `requestDelete` | Delete | action (danger) | A required reason is checked on Confirm. | "deletes only with a reason, keeps the dialog on failure, and retries" |
| `resumeUser` | `handlers.onResume` via `UserTable.tsx` `requestResume` | Resume | action | | "resumes a deleted user…" |
| `hardDeleteUser` | `handlers.onHardDelete` via `UserTable.tsx` `requestHardDelete` | Permanently Delete | action (danger) | The typed confirmation is kept as `requireText` (the displayed account identifier). | "requires the displayed account identifier…", "cancelling a permanent delete writes nothing" |
| `analyzeUsageInsights` | `users/UserUsageInsights.tsx` `analyze` | Analyze usage / Re-analyze | action | It stores no result, but it is a paid LLM call over up to 100 of the user's prompts and writes an audit row. The dialog says so. | "asks before the paid analysis…", "runs the analysis once on Confirm…", "keeps the confirmation open with the error…" |
| `sampleUsageInsights` | `users/UserUsageInsights.tsx` `viewTurns` | View user turns | read-exempt | It reads a sample, with an audit entry only. | "views user turns directly…" |
| saved views, density, page size, columns | `users/hooks/useSavedViews.ts`, `users/index.tsx` | View controls | read-exempt | `localStorage` view preferences, not server writes. | — |

### Providers (`ProviderKeysTab.tsx`, `ProviderOverviewTab.tsx`, `components/features/admin/ProvidersTab.tsx`)

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `addProviderKey` | `ProviderKeysTab.tsx` `onReviewAdd` | Add key → form → Review key → Add key | create | The review shows provider, label, tier and "API key: Set (hidden)". A typed key is masked as `••••` in any error (shared `withoutSecret`). A successful add switches the page to the key's provider, so Add key, and Review key in a form opened earlier, wait while a tier edit is open. | "opens the add form only from Add key…", "reviews the new key without its secret…", "keeps the add review open with a masked error…", "holds Add key until the tier edit is saved or cancelled", "holds the review of a key for another provider while a tier edit opened after it is open" |
| `verifyProviderKey` | `ProviderKeysTab.tsx` (Verify in the add form) | Verify | read-exempt | The backend dry-run stores nothing. | "verifies a key directly, without a confirmation" |
| `setProviderKeyMinRole` / `setProviderEnvKeyMinRole` | `ProviderKeysTab.tsx` `saveTiers` | Edit tiers → Save changes → Confirm save | edit | One request per key through `writeEach`. The provider selector and adding a key (which switches provider) are locked while editing. | "shows reservations read-only until Edit…", Cancel, no-op, "re-tiers a key… only after the change is confirmed", busy, failure, "reports a partial tier save and retries only the unsaved key" |
| `setProviderKeyStatus` | `ProviderKeysTab.tsx` `requestDbStatus` | Disable/Enable key `<prefix>` | action | | "disables a DB key after confirmation" |
| `disableProviderEnvKey` / `enableProviderEnvKey` | `ProviderKeysTab.tsx` `requestEnvStatus` | Disable/Enable env key | action | `window.confirm` is replaced. | "disables an env key after confirmation, and Cancel writes nothing", "re-enables a disabled env key after confirmation" |
| `deleteProviderKey` | `ProviderKeysTab.tsx` `requestDelete` | Delete key `<prefix>` | action (danger) | `window.confirm` is replaced. | "deletes a DB key only after the named confirmation", "keeps the delete confirmation open with the error…" |
| `setProviderDisabled` | `ProvidersTab.tsx` `QuotasSection` `handleToggle` (quota cards), `AvailabilitySection` `handleToggle` (availability table) | Provider switch | action | The dialog names the provider and the routing impact. | `__tests__/ProvidersTab.quotas.test.tsx`: "disables a provider only after a confirmation that names it", "leaves the provider untouched when its confirmation is cancelled", "re-enables a provider after confirmation and keeps the dialog on failure" |
| `setProviderQuotaKeyDisabled` | `ProvidersTab.tsx` `QuotasSection` `handleToggleKey` | Key switch on a quota card | action | `window.confirm` is replaced; enabling also asks. | "disables a single key after confirmation…", "leaves the key untouched when the confirmation is cancelled", "re-enables a disabled key after its own confirmation", "keeps the key confirmation open with the error, blocks a second submit, and retries" |
| `probeProviderDefinition` | `ProviderOverviewTab.tsx` (Probe) | Probe | read-exempt | The backend verify calls the upstream without saving. | "probes directly, then reviews…" |
| `createProviderDefinition` | `ProviderOverviewTab.tsx` `AddProviderModal` `onSubmit` | Add Provider → Review provider → Add provider | create | Every field is reviewed, with the key hidden. The confirmation stacks over the modal. | "probes directly, then reviews the new provider without its key…", "keeps the review open with a masked error…" |
| `updateProviderDefinition` | `ProviderOverviewTab.tsx` `EditProviderModal` `onSubmit` | Edit `<name>` → Save changes → Confirm save | edit | A new Base URL must be probed first; the probe key is never shown. | "shows the saved values read-only until Edit…", Cancel, old→new/Back/Confirm, busy, failure, "requires a probe for a new Base URL…" |
| `deleteProviderDefinition` | `ProviderOverviewTab.tsx` `requestDelete` | Delete `<name>` | action (danger) | The typed slug confirmation is kept (`requireText`). | "writes nothing on Cancel and deletes once after the slug is typed", "keeps the delete confirmation open with the error…" |

### Announcements (`SiteUpdatesTab.tsx`, `BroadcastsTab.tsx`)

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `createSiteUpdate` | `SiteUpdatesTab.tsx` `reviewCreate` | New update → form → Create Update → review → Create and publish / Create draft | create | The composer is hidden until opened. "Title is required" is still checked. The review says whether the update goes live and where. | `SiteUpdatesTab.test.tsx` (33 tests: 17 written for this PR, 13 from #1640 and 3 added in its merge): "reviews what will be published, keeps the form on Back, and creates once on Confirm", "keeps the review open with the error when the create fails, and retries", … |
| `updateSiteUpdate` (fields) | `SiteUpdatesTab.tsx` `reviewEdit` | Edit → Save changes → Confirm save | edit | Only the changed fields are sent (the backend accepts partial PATCH, `exclude_unset`). | "lists old -> new, keeps drafts on Back, and writes only the changed fields once", busy, failure |
| `updateSiteUpdate {published}` | `SiteUpdatesTab.tsx` `confirmTogglePublished` | Publish / Unpublish | action | | "confirms an unpublish naming the update…", "confirms a publish with where it appears" |
| `deleteSiteUpdate` | `SiteUpdatesTab.tsx` `confirmRemove` | Delete | action (danger) | Native `confirm()` is replaced. | "uses the shared danger dialog instead of window.confirm", "keeps the dialog with the error when the delete fails, and retries" |
| `createBroadcast` | `BroadcastsTab.tsx` `reviewSend` | Send Now / Schedule | action (send) | The dialog gives the recipient count from a current preview (fetched first if stale), the subject, filters and timing. Spend validation is kept. **Schedule is disabled until a time is chosen**: previously an empty time sent at once. | `BroadcastsTab.test.tsx` (23 tests: 12 written for this PR, 8 from #1640 and 3 added in its merge): "confirms with the previewed count and timing…", "counts the recipients first when there is no preview…", busy, failure, "needs a time, then confirms the schedule…" |
| `sendTestBroadcastEmail` | `BroadcastsTab.tsx` `reviewTestSend` | Send Test to Me | action (test-send) | The dialog names the recipient (the signed-in admin's email). | "names the recipient; Cancel sends nothing; Confirm sends one test" |
| `cancelBroadcast` | `BroadcastsTab.tsx` `confirmCancel` | Cancel (scheduled) | action (danger) | Native `confirm()` is replaced. The dialog names the subject, the count and the time. | "replaces window.confirm, names the broadcast, and cancels once on Confirm" |
| `previewBroadcast` | `BroadcastsTab.tsx` `countRecipients` | Preview & Count | read-exempt | The backend renders and counts (`SELECT COUNT(*)`) with no insert. | "runs Preview & Count directly…" |

### Routing (`ProviderRoutesTab.tsx`, `OffloadRoutePanel.tsx`, `RoutewiseSettingsPanel.tsx`)

| API | Call site | UI trigger | Rule | Coverage | Tests |
|---|---|---|---|---|---|
| `updateProviderRoute` | `ProviderRoutesTab.tsx` `onSubmit` (route editor) | Route editor → Save changes → Confirm save | edit | The review stacks over the editor. The editor's concurrency and quota validation is kept. | `ProviderRoutesTab.test.tsx`: "lists the changed fields and saves nothing until the save is confirmed", "keeps Save disabled for an unchanged route, and Cancel closes it with zero writes", "refuses a zero concurrency limit before the review", failure+retry, busy |
| `createProviderRouteModel` / `createProviderRouteCandidate` | `ProviderRoutesTab.tsx` `onCreateSubmit` | Create model / Add provider → review | create | Everything to be created is reviewed: pricing for a model, and the capabilities that a new model and an added route each declare (since #1630), including audio or video kept as declared. | "reviews what a new route will add, keeps the form on Back, and adds it once", "reviews a new model before creating it and keeps a failed create retryable", "starts an added route with the audio and video the first route accepts" |
| `verifyProviderRoute*` | `ProviderRoutesTab.tsx` (Verify buttons) | Verify | read-exempt | The backend pings once and persists nothing. | "verifies provider route target without applying it", … |
| `deleteProviderRoute` | `ProviderRoutesTab.tsx` `confirmResetYaml` | Reset config | action (danger) | | "resets an override to the config route" |
| `deleteProviderRouteCandidate` | `ProviderRoutesTab.tsx` `confirmDeleteRuntimeRoute` | Delete (runtime route) | action (danger) | | "asks before deleting a runtime route", "keeps a failed row action in its dialog, and Cancel writes nothing" |
| `setRouteWeight(…, 0)` / `clearRouteWeight` | `ProviderRoutesTab.tsx` `confirmDisableRoute` / `confirmEnableRoute` | Disable / Enable (RouteWise config route) | action | | "disables and enables routewise config routes…", "asks before disabling a route, and Cancel writes nothing" |
| `clearRouteWeight` | `ProviderRoutesTab.tsx` `confirmClearWeight` | Weight Reset | action (danger) | The dialog shows `weight → configured weight`. | "edits fixed route weights after Edit and confirms each change" |
| `updateProviderRouteStrategy` | `ProviderRoutesTab.tsx` `saveStrategy` | Edit routing policy → Save | edit | Previously written when the select changed. A switch unmounts the panel of the current policy (Queue offload for fixed, RouteWise parameters for routewise), so Edit is held while either has an edit open, and Save waits while one opened after it is still open. | "updates the selected model routing policy", "cancels a routing policy edit with zero writes", "holds the routing policy while RouteWise parameters are being edited", "holds the routing policy while a Queue offload draft is open", "holds a policy switch while a parameter edit opened after it is open" |
| `setRouteWeight` | `ProviderRoutesTab.tsx` `saveWeights` | Edit weights → Save | edit | Previously saved directly per row. Now one request per changed route through `writeEach`. | "edits fixed route weights after Edit…", "cancels weight edits with zero writes", "refuses a negative weight before the review", "reports a partial weight save and retries only the unsaved weight" |
| `updateProviderRouteCandidate` | `ProviderRoutesTab.tsx` `saveConcurrencyLimit` | Concurrency limit → Edit → Save | edit | Previously written directly. The positive-integer check is kept. | "updates a runtime OpenRouter concurrency limit after Edit and confirmation", "refuses an invalid concurrency limit before the review" |
| `setOffloadRoute` | `OffloadRoutePanel.tsx` `onSave` | Edit Queue offload → Save | edit | Previously written directly. All limit validations are kept. | `OffloadRoutePanel.test.tsx` (23 tests) |
| `clearOffloadRoute` | `OffloadRoutePanel.tsx` `onClear` | Clear offload route | action (danger) | Previously written directly. | "shows the stored route read-only and clears it only after confirmation" |
| `updateRoutewiseSetting` | `RoutewiseSettingsPanel.tsx` `handleSave` | Edit RouteWise parameters → Save | edit | One request per setting. Stale responses after a model switch are still ignored. | `RoutewiseSettingsPanel.test.tsx` (17 tests) |
| `resetRoutewiseSetting` | `RoutewiseSettingsPanel.tsx` `handleResetSetting` | Reset | action (danger) | | "resets only an explicit override, after confirmation…" |
| `runRoutewiseProbe` | `RoutewiseSettingsPanel.tsx` `runProbe`, confirmed by `handleRunProbe` | Run probe | action | Not read-exempt: it sends live traffic, stores samples, updates router state and is audited. The dialog says that it uses provider quota. | "asks before sending live probe traffic and names the endpoint", "keeps a failed probe run in the dialog…" |

`RouteWeightTable.tsx`, which was dead code, has been removed on dev (#1649). `RouteDeploymentField.tsx`, `UpstreamConcurrencyPanel.tsx`, `RoutewiseDecisionsPanel.tsx` and `RoutingTab.tsx` make no writes.

## Bypass review

For each write function in `lib/api/admin.ts` and `lib/api/config.ts`, every call site under the two admin trees was listed and traced to its enclosing context:
- Each one is inside a confirmed `run`, or inside a handler that only a confirmed `run` calls. That covers `users/index.tsx` handlers called from `UserTable.tsx` and `UserDetailPanel.tsx` `run`s, and `ConfigurationTab.commitSave`, which only the dialog's Confirm reaches.
- No `onChange` or `onBlur` handler in these trees calls a write. The callees of the remaining `onChange` handlers only set drafts, filters, columns or create-form fields.
- No `window.confirm` / `confirm()` call and no bespoke confirmation overlay remains in the admin trees. The remaining modal overlays are editors or viewers, not confirmations: the route editor, capabilities editor and create forms in `ProviderRoutesTab.tsx`, the provider add/edit forms in `ProviderOverviewTab.tsx`, the saved-view name in `users/SavedViews.tsx` (local only) and `geo/DataDetailsDialog.tsx` (read). Their confirmations render after them in the DOM, so they stack on top.

Legitimate exceptions, all read-exempt:
- verifications and probes that store nothing: `verifyProviderKey`, `probeProviderDefinition`, `verifyProviderRoute*`;
- previews: `previewBroadcast`, `previewRoleQuotaApply`;
- `sampleUsageInsights` (a read with audit);
- `exportRequests` (GET download);
- `localStorage` view preferences.

The setup wizard (`app/setup/ConfigureStep.tsx`) is outside the Admin Console and is unchanged.

## Behaviour changes worth noting for review

- Write failures now show inside the open dialog, where they can be retried, instead of in an error toast. The Configuration category save is the exception described under Multi-request saves: its review closes and the reason is shown beside its Save. Success toasts are unchanged.
- Enabling something (a key, an env key, a provider) now asks too, as the rules treat enable/disable as one action.
- The Site updates composer, the signup-domain input, Add variable, the provider-key form and the provider-definition form open only from an explicit Add/New button.
- Users: the account-settings double PATCH is fixed. Required reasons (reject, delete) are checked on Confirm, and the message is shown in the dialog.
- Broadcasts: Schedule needs a time.
- Site updates: an edit sends only the changed fields.
- Route editor: closes after a successful save.
- The model selector, and Create model (which switches to the new model), are locked while any routing edit or confirmation is open: weights, policy, concurrency limit, Queue offload or RouteWise parameters. So is the routing-policy Edit, since a switch unmounts the Queue offload or RouteWise panel; a policy edit opened first cannot be saved while one of those panels is being edited. The provider selector is locked while a tier edit is open, and so is adding a key, which switches to the key's provider when it succeeds. So drafts are never applied to, or silently dropped for, another target.

## Follow-up after browser acceptance (2026-10-24)

The browser review found two boundary issues. Both are fixed, and each has a regression test that fails against `0acc4b73`.

1. **Emptied daily quota** (`users/UserDetailPanel.tsx`, `quotaError` / `accountPatch`).
   - **The bug.** Clearing a non-empty "Daily quota (USD)" computed `Number('')` and reviewed `10 → 0`.
   - **The fix.** A changed quota that is empty, whitespace or negative is now invalid. It counts as an unsaved change and is named under the field (`aria-invalid`). Save shows "Correct the daily quota before saving: …" and opens no review, so nothing is written.
   - **Unchanged.** An explicit `0` stays valid (the API takes `ge=0`). A user with no saved quota keeps it out of a save that changes other fields.
   - **Tests** (`UserTable.test.tsx`):
     - "refuses an emptied daily quota instead of reviewing it as 0"
     - "refuses a negative daily quota"
     - "still allows a deliberate zero quota"
     - "leaves an unsaved (null) quota out of a save that changes other fields"
   - The first two fail on 0acc4b73. The last two pass on both, so they guard the semantics that were kept.
2. **Model navigation guard for the child editors** (`ProviderRoutesTab.tsx`, `OffloadRoutePanel.tsx`, `RoutewiseSettingsPanel.tsx`, new `confirm/useReportEditing.ts`).
   - **The bug.** Both panels reset their drafts when `modelId` changes, but the parent only locked the selector for its own edits.
   - **The fix.** Each panel now reports "edit or confirmation open" through `onEditingChange`, and reports `false` again on unmount. The parent's `modelLocked` combines this with its own edits, and disables the selector and Create model.
   - **Unchanged.** The panels' model-change resets and stale-response guards are kept.
   - **Tests** (`ProviderRoutesTab.test.tsx`):
     - "holds the model selection while an offload edit or its review is open" (also covers Back, then Cancel, then a switch with fresh drafts)
     - "releases the model selection once an offload save is confirmed"
     - "holds the model selection while RouteWise parameters are being edited"
   - All three fail on 0acc4b73.
   - **Hook test:** `confirm/useReportEditing.test.tsx` checks that unmounting mid-edit releases the parent.
   - **Stale-response tests still passing** (`RoutewiseSettingsPanel.test.tsx`):
     - "ignores stale probe data and probe completion after the model changes"
     - "reloads on model changes and ignores a stale response from the old model"
     - "ignores a stale mutation after switching away from and back to the same model"

A suspected clipping of the UserUsageInsights confirmation inside the Users table was not reproduced: in the browser, the overlay covered the viewport. It was left unchanged, as were the existing UserTable confirmation placement and typed confirmations.

## Integration with dev at 8170f5e5 (#1640, plus #1630) — 2026-10-25

`dev` (8170f5e5, "unify console styling and selection controls") was merged into this PR (prior head 4b79a946) with a normal merge. 25 files conflicted: 16 implementation files and 9 test files.

**What the merged tree keeps**

- **From #1640, everything:**
  - the shared Admin palette (`adminStyles.ts`) and primitives (`AdminSection`, `AdminSectionNav`, `AdminNotice`, …);
  - the full-width shell with category and section indexes, deep links and active markers;
  - compact, expandable announcement and email summaries, Markdown previews, draft retention and no publish on Enter;
  - the shared `components/ui/Select`.
- **From #1630:** route capabilities. Its two writes are listed under New write call sites below, and the create review now lists the capabilities an added route declares.
- **From this PR:** every rule in the sections above.
- **Dependencies:** the manifests and lockfiles are byte-identical to dev (filelock 4.2.4, zod 4.6.7, Next.js 15.5.28). The merge changes nothing outside `apps/frontend/src` and this document.
- **Native controls:** no native `<select>` remains in frontend source.

**Semantic resolutions**

| Area | Resolution |
|---|---|
| Shared controls | `EditBar`/`EditButton` now use `BUTTON_PRIMARY`/`BUTTON_GHOST`/`BUTTON_SECONDARY`. In edit mode every select is the shared `Select`, and its `onValueChange` only updates a draft. |
| `ModelVisibilitySection` | dev wrote from `onValueChange`. Now: `AdminSection` layout, read-only `<output>` until Edit, then a `Select` that changes the draft only, and `writeEach` after Confirm. dev's "browse/dismiss writes nothing" and "reselecting the current value is no change" tests are kept and adapted to edit mode. dev's immediate-write tests are superseded by the confirm-flow tests (old→new, one write per changed model, busy, failure+retry, partial+retry). |
| `ModelConcurrencySection`, `UsageInsightsSettingsSection`, `AlertSnoozeSection` | dev's `AdminSection` layout and palette, with this PR's edit or action flow. |
| `AlertMutesSection` | dev's command-style `Select` (value stays empty, so the same duration can be chosen again). A choice opens the mute confirmation. dev's "mutes again when the same duration is chosen twice" now confirms each time and writes once per confirmation. |
| `SettingsTab` | Rebuilt on dev's indexed page (section index, floating live feedback, panel lists, styled domain table). Feature flags and numeric settings stay in `RuntimeSettingsSection` (edit flow, one request per setting). Add domain is a create review; Remove and Apply quota are action confirmations. dev's bespoke overlays and immediate toggle/numeric writes are not kept. dev's feedback and section-index tests now go through Edit → Confirm. |
| `ConfigurationTab`, `ConfigEntryField`, `AlertNotificationFields` | dev's category index, anchors, split layout and panel, with this PR's read-only/Edit categories, staged Reset and Mark as secret, hidden-by-filter note and sticky edit bar. The index's unsaved marker uses this PR's count (each variable once, staged actions included), so it agrees with the edit bar. dev's index test now edits first and also checks that Cancel clears the marker. |
| `UserDetailPanel` | This PR's `AccountSettings` editor, with dev's role `Select` (draft only). The empty-quota validation and the single PATCH are unchanged. |
| `OffloadRoutePanel` | This PR's read-only/Edit flow and `useReportEditing`, with dev's route `Select` in edit mode. |
| `ProviderRoutesTab` | dev's searchable model `Select`, grouped OpenRouter routing and #1630 capability fields and badge. `modelLocked` now also covers the route editor, the capability dialog, the create form and any open confirmation. dev's immediately-writing policy select is replaced by this PR's Edit flow. |
| Route capabilities (#1630) | The dialog fields are drafts. Save opens an old→new review and only Confirm calls `updateProviderRouteCapabilities`. Reset to config is a danger confirmation that lists current → config. Errors use `routeErrorMessage`, inside the dialog. |
| `ProviderKeysTab` | All four selects are `Select`. Tier choices are drafts (amber border). The provider selector is locked during a tier edit. A native form-provider `<select>` that auto-merged in was converted. |
| `SiteUpdatesTab` | Built on dev (editor that opens when needed and keeps its draft, Markdown preview, no submit on Enter), with this PR's create review, changed-fields-only edit, publish and delete confirmations. dev's "ask before discarding an unsaved draft" now uses the shared dialog instead of `window.confirm`, and replaces this PR's lock on other rows' Edit buttons. |
| `BroadcastsTab` | Built on dev (composer, template `Select`, recipients drawer), with this PR's send/schedule confirmation (current recipient count, timing), test-send recipient, cancel confirmation, and Schedule needing a time. dev's own confirm modal and `confirm()` were removed. |
| add/add tests (`SiteUpdatesTab.test.tsx`, `BroadcastsTab.test.tsx`) | One file each with every test from both sides: 13+17+3 and 8+12+3. |

**New write call sites since the inventory above**

| API | Call site | Trigger | Rule | Tests |
|---|---|---|---|---|
| `updateProviderRouteCapabilities` | `ProviderRoutesTab.tsx` `onSaveCapabilities` | Capabilities → edit fields → Save capabilities → Confirm save | edit | `ProviderRoutesTab.test.tsx`: "edits a route from what it serves now, writing only after the review", "drafts nothing from browsing or reselecting a capability, and Cancel discards", "says which value blocks saving", "blocks a second Confirm and closing while a capability save is in flight", "keeps the audio and video a route accepts when saving its other values", "shows the gateway's reason when a capability change is refused, and keeps the draft", "holds the model selection while a capability edit is open" |
| `resetProviderRouteCapabilities` | `ProviderRoutesTab.tsx` `confirmResetCapabilities` | Capabilities → Reset to config → Reset | action (danger) | `ProviderRoutesTab.test.tsx`: "resets an edited route to its config values only after the confirmation" |

**Bypass review (re-run on the merged tree)**

- All 71 write call sites under the two admin trees were traced. Each one is inside a confirmed `run`, or in a handler only a `run` reaches: the `users/index.tsx` handlers, `ConfigurationTab.commitSave` (only the review's `onConfirm` calls it) and the `ProvidersTab` toggles (through `providerToggleRequest`).
- The exceptions are read-exempt: probe and verify calls, and the broadcast preview.
- Every `onChange` and `onValueChange` in the admin trees updates local state, filters or drafts. AlertMutes' `onValueChange` opens a confirmation.
- No `window.confirm`/`confirm()` remains.
- Raw evidence: `/tmp/pr1629-dev-integration-logs/30-native-and-trigger-audit.txt` and `31-write-callsite-trace.txt`.
