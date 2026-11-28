# Translating the Docs and the Console

This page is for translators, and for anyone whose English edit changes
translated text. The docs and the console are translated separately: the docs
through Sphinx's gettext catalogs, the console through a JSON catalog. Both
show English for anything untranslated, and both follow the
[terminology tables](#chinese-terminology) at the end.

## Translating the docs

Each translation is attached to one paragraph of English source, not to a whole
file, so a partly translated page still builds complete.

```bash
make docs-gettext                      # extract one catalog template per page
make docs-translate DOCS_LANG=zh_CN    # create or update that language's catalogs
# edit docs/developer/locale/zh_CN/LC_MESSAGES/*.po -- fill in msgstr
make docs-lang DOCS_LANG=zh_CN         # build it and read the result
```

A catalog entry pairs the English source with its translation:

```po
#: ../index.rst:4
msgid "HybridInference is an open-source LLM inference gateway."
msgstr "HybridInference 是一个开源的 LLM 推理网关。"
```

The English text is the lookup key. After you edit a paragraph, the next
`make docs-translate` marks its entry `#, fuzzy`, the build stops using it, and
the page shows the English until someone updates the translation. Translators
only revisit marked entries. Use catalogs, not parallel `.zh.md` files: those
drift with no sign that they are out of date.

Commit the `.po` files; `docs/gettext/` is generated and ignored. You do not
have to translate a whole page or keep a language complete.

### Traps in Chinese, Japanese and Korean

Each of these leaves the build green and the page wrong.

**A heading that starts with a number is dropped.** MyST re-parses a translated
title and reads `1. ` as a list marker. The structure no longer matches the
source, so Sphinx emits the English heading, with no warning even under `-W`.
Escape the period:

```po
msgstr "1\. 申请一个节点"
```

**In `index.rst`, inline markup that touches a CJK character does not parse.**
reStructuredText needs whitespace or certain punctuation before an opening `*`,
so `请求的*模型 id*` renders literal asterisks. Separate them with an escaped
space, written `\\ ` in the catalog:

```po
msgstr "客户端请求的\\ *模型 id*\\ ，与真正服务它的\\ *端点*\\ 是解耦的。"
```

Markdown pages need no such escaping: CommonMark treats a CJK character as
neither whitespace nor punctuation, so `**模型 id**` between Chinese characters
works.

**In Markdown, a closing `**` after a CJK full stop breaks** when a CJK
character follows it, so `**术语。**后文` leaves literal asterisks. Put the full
stop outside the bold: `**术语**。后文`.

**A stale `.mo` hides your edits.** Sphinx does not recompile a `.mo` that is
newer than its `.po`. Before any verification build, run:

```bash
find docs/developer/locale -name '*.mo' -delete
```

A fifth problem fails loudly: `make docs-translate` can add `python-format` to
an entry marked `no-python-format`, because a string such as `≥ 5%` looks like
a format string, and `msgfmt -c` then rejects the pair. Delete the added
`python-format`.

### Checking the docs

```bash
make docs-verify
```

The Docs Build CI job runs the same command. It builds every language with
warnings as errors, extracts fresh catalog templates, checks local links, and
runs `ops/ci/check_docs_translations.py`. That last step is needed because
Sphinx falls back to English for any string it cannot translate, so a page that
has quietly reverted still builds clean. It fails on:

- **fuzzy** entries, which Sphinx does not apply;
- **missing catalogs**, for a page added without `make docs-translate`;
- **stale catalogs**: an English edit whose `msgid` no longer matches any
  entry. This is the commonest case, and it has no `fuzzy` marker;
- **block starts**: a translation that begins with `1. `, `- `, `# ` or `> `,
  which re-parses as a list or a heading and is dropped;
- **structural divergence**: different counts of `<code>`, `<strong>`, `<em>`
  and `<a>`, or different inline-code literals and link targets, between a
  translated page and the English one. This catches the markup traps above.

### How the languages are published

`make docs` builds every language in one `sphinx-build` run. `DOCS_LANGUAGES`
in `conf.py` lists them (default `en:English,zh_CN:简体中文`). The first is the
root language, at the top of the output tree, so `/routing.html` keeps its URL
and the translation is at `/zh_CN/routing.html`. A `build-finished` hook writes
each other language to `docs/build/html/<code>/`. The list lives in `conf.py`
because the published site is built by a command in the hosting project's
settings, where no environment variable can be set.

The sidebar switcher links to the same page in the other languages and is
hidden when only one language is declared. `DOCS_LANGUAGES="en:English"` builds
English alone.

## Translating the console

The console can be read in English or Chinese. A signed-in reader chooses
**Language** from the account menu or under **Account Settings**; the choice is
kept in the browser and in the account, so it follows them to other browsers.
A signed-out reader sees the language last chosen in that browser, or the one
the browser asks for. The **Appearance** setting (**Light**, **Dark** or
**System**) is saved per browser only.

### Writing a console string

Every string a reader sees goes through `t(slot, English)`:

```tsx
const t = useT();
<h2>{t('settings.language.title', 'Language')}</h2>
```

- The English at the call site is the source text. Edit it there; there is no
  English catalog.
- Name the slot `<area>.<page or component>.<meaning>`, like
  `auth.login.title` and `chrome.header.dashboard`.
- Translate whole sentences, and insert values with `fill()` from
  `@/lib/utils/interpolate`, so another language can reorder them:
  `fill(t('chrome.account_menu.switched', 'Switched to {name}'), { name })`.
- In a server component, get the same function, in the request's language,
  from `await requestTranslator()` (`@/lib/i18n/request.server`).
- Format dates and numbers with `useFormatter()` (`@/lib/i18n/useFormatter`),
  not `toLocaleString()`. Amounts stay in US dollars.
- Both arguments must be literals (or a `cond ? 'a' : 'b'` of literals), so the
  catalog check can read them.

### The catalog

`apps/frontend/src/lib/i18n/messages/zh-CN.json` maps each slot to the English
it translates and the translation:

```json
"settings.language.title": { "source": "Language", "text": "语言" }
```

An entry is used only while its `source` matches the English the call site
passes, so a changed sentence shows the new English until it is translated
again. A slot that passes different English in different places holds a list
of entries, one per string. Only the active language's catalog is sent to the
browser.

```bash
cd apps/frontend
npm run i18n:check                                 # report every catalog
node scripts/i18n/check-catalogs.mjs --todo zh-CN  # what still needs translating
node scripts/i18n/check-catalogs.mjs --prune       # drop slots no code uses
```

Missing and stale entries fall back to English. The test suite
(`src/lib/i18n/catalog.test.ts`) fails on a malformed entry, on a translation
whose `{placeholders}` differ from its source, and on stale or orphaned entries
in a shipped catalog, so re-translate or prune in the same change that edits
the English.

The Terms of Service and the research-consent statements at sign-up are
translated in full and shown in Chinese alone. Because they are binding, an
edit to their English must come with its translation in the same change.

### What stays in English

- Pages only the people running the deployment see: administration
  (`/dashboard/admin`), first-run setup (`/setup`), and the API Playground,
  which only internal accounts can open. The admin layout provides an English
  `LocaleProvider` with no catalog and marks its container `lang="en"`, and
  admin dates use the `en-US` locale.
- The last-resort error screens (`global-error` and the boundary around the
  whole application), which render outside the language provider.
- Error messages from the gateway, model names, and Markdown written by
  administrators (broadcasts, site updates).

## Chinese terminology

Use the same Chinese for an English term everywhere, and define a new term in
the [Glossary](glossary.md) first. Terms that stay in English are written in
English in the Chinese text too.

In the docs:

| English | Chinese |
|---|---|
| model id | 模型 id |
| alias | 别名 |
| model registry | 模型注册表 |
| route | 路由 |
| endpoint | 端点 |
| upstream | 上游 |
| weight | 权重 |
| fallback | 回退 |
| circuit breaker | 熔断器 |
| distribution | 发行版 |
| dry run | 试运行 |
| console | 控制台 |
| operational store | 运行数据存储 |
| setting | 设置项 |
| first-run setup | 首次运行设置 |
| setup code | 设置码 |
| provider, kind, adapter, router, overlay, manifest | provider、kind、adapter、router、overlay、manifest |

In the console:

| English | Chinese |
|---|---|
| Dashboard (the console) | 控制台 |
| Log in / Sign up | 登录 / 注册 |
| Usage | 用量 |
| Quota | 额度 |
| Docs Assistant | 文档助手 |
| Status (page) | 服务状态 |
| API key, token, model id, endpoint, Playground | unchanged |
