# Translating the Console

The console can be read in English or Chinese. A reader who never chose a
language gets the one their browser asks for. Signed-in readers choose Language
from the account menu; signed-out readers use the header switcher. The language
setting also remains available under Account Settings. The console keeps the
most recent choice in this browser and, once they sign in, in their account,
so it follows them to other browsers. The design record is
[2026-10-17 console language switching](https://github.com/HarvardMadSys/hybridInference/blob/dev/docs/agents/specs/2026-10-17-console-language-switching-design.zh.md).

This page is for anyone who adds console text or translates it. Translating
these pages, the documentation, is a separate workflow:
[Translating the Docs](translations.md).

## Appearance preferences

The account menu also offers Appearance: Light, Dark, or System. System is the
default and follows the operating system's appearance. An explicit selection is
saved in the current browser, independently of the account's language preference;
it does not follow the account to another browser. The theme applies to the
console; pages owned by a Site UI module keep that module's appearance.

## Writing a console string

Every string a reader sees goes through `t(slot, English)`:

```tsx
const t = useT();
<h2>{t('settings.language.title', 'Language')}</h2>
```

- The English at the call site is the source text. Edit it there; there is no
  English catalog to keep in step.
- Name the slot `<area>.<page or component>.<meaning>`, like the existing
  `auth.login.title` and `chrome.header.dashboard`.
- Translate whole sentences, and put values in with `fill()` from
  `@/lib/utils/interpolate`, so a language with a different word order can move
  them: `fill(t('chrome.account_menu.switched', 'Switched to {name}'), { name })`.
- A server component gets the same function, in the request's language, from
  `await requestTranslator()` (`@/lib/i18n/request.server`).
- Dates and numbers go through `useFormatter()` (`@/lib/i18n/useFormatter`)
  rather than `toLocaleString()`, so a Chinese page does not print an English
  month beside its words. Amounts stay in US dollars.
- Both arguments must be literals (or a `cond ? 'a' : 'b'` of literals), so the
  catalog check can read them.

Administration pages (`/dashboard/admin`) are for operators, not users, and stay
in English: they do not go through `t()`, and their container is marked
`lang="en"`. The rest of what stays in English is listed below.

## The catalog

`apps/frontend/src/lib/i18n/messages/zh-CN.json` maps each slot to the English it
translates and the translation:

```json
"settings.language.title": { "source": "Language", "text": "语言" }
```

The runtime uses an entry only while its `source` is exactly the English the
call site passes. Changing a sentence therefore shows the new English until it
is translated again, instead of a translation of what the page used to say. A
slot that passes different English in different places holds a list of
entries, one per English string.

Only the active language's catalog is sent to the browser, as part of the page;
English has none.

## Checking a catalog

```bash
cd apps/frontend
npm run i18n:check                                 # report every catalog
node scripts/i18n/check-catalogs.mjs --todo zh-CN  # what still needs translating
node scripts/i18n/check-catalogs.mjs --prune       # drop slots no code uses
```

The report counts translated, missing, stale (the English changed) and
orphaned entries. Missing and stale entries fall back to English and do not
fail anything. The test suite (`src/lib/i18n/catalog.test.ts`) does fail on a
malformed entry, on a translation whose `{placeholders}` differ from its
source, and on stale or orphaned entries in a shipped catalog — re-translate or
prune in the same change that edits the English.

## What stays in English

- What only the people running the deployment see: administration
  (`/dashboard/admin`), first-run setup (`/setup`), the Configuration tab, and
  the API Playground, which only internal accounts can open.
- The last-resort error screens (`global-error` and the boundary around the
  whole application), which render outside the language provider.
- Error messages from the gateway, model names, and Markdown written by
  administrators (broadcasts, site updates).

The Terms of Service and the research-consent statements at sign-up are
translated in full, and the Chinese is shown on its own, not as a reference
beside the English. Because they are binding, an edit to the English text must
come with its translation in the same change; the catalog test refuses a stale
entry.

## Chinese terminology

Keep one Chinese term per English term across the console, as the docs do (see
[Chinese terminology](translations.md#chinese-terminology)).

| English | Chinese |
|---|---|
| Dashboard (the console) | 控制台 |
| Log in / Sign up | 登录 / 注册 |
| Usage | 用量 |
| Quota | 额度 |
| Docs Assistant | 文档助手 |
| Status (page) | 服务状态 |
| API key, token, model id, endpoint, Playground | unchanged |
