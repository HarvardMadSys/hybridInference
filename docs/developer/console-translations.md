# Translating the Console

The console can be read in English or Chinese. A reader who never chose a
language gets the one their browser asks for; once they choose — from the
header, or under Account Settings — the console keeps their most recent
choice, in this browser and, once they sign in, in their account, so it
follows them to other browsers. The design record is
[2026-10-17 console language switching](../agents/specs/2026-10-17-console-language-switching-design.zh.md).

This page is for anyone who adds console text or translates it. Translating
these pages, the documentation, is a separate workflow:
[Translating the Docs](translations.md).

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
- A server component uses the same pair through `translate` from
  `@/lib/i18n/translate`.
- Both arguments must be literals (or a `cond ? 'a' : 'b'` of literals), so the
  catalog check can read them.

Administration pages (`/dashboard/admin`) are for operators, not users, and stay
in English: they do not go through `t()`, and their container is marked
`lang="en"`.

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

- The Terms of Service and the research-consent statements at sign-up. They
  are binding text; a translation needs review by whoever owns the terms and
  the study, so the catalog leaves them out on purpose.
- Error messages from the gateway, model names, and Markdown written by
  administrators (broadcasts, site updates).

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
