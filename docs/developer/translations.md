# Translating the Docs

This page is for translators, and for anyone whose English edit changes a
paragraph that has a translation.

The pages are written in English and translated with Sphinx's gettext
workflow, so a translation is attached to *each paragraph of source text*
rather than to a whole file. That is what makes a partial translation safe:
any string without one falls back to English, and the site still builds
complete.

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

Because the English text *is* the lookup key, editing a paragraph invalidates
its translation automatically: the next `make docs-translate` marks that entry
`#, fuzzy`, the build stops using it, and the page falls back to English rather
than serving a translation that no longer matches what the code does. A
translator only has to revisit the entries that are marked. **This is the
reason to use catalogs instead of parallel `.zh.md` files**, which drift
silently and give a reader no signal that what they are reading is out of date.

Commit the `.po` files. `docs/gettext/` is generated and ignored.

Translating a page does not require translating all of it, and there is no
obligation to keep a language complete — an untranslated paragraph is a
fallback, not a bug.

## Translating into Chinese, Japanese or Korean

Four traps, all of them silent — the build stays green and the page is wrong.

**A heading that starts with a number is discarded.** Sphinx re-parses a
translated title, and MyST reads `1. ` as an enumerated-list marker rather than
text. The structure no longer matches the source, the translation is dropped,
the English heading is emitted, and nothing warns — not even under `-W`. Escape
the period:

```po
msgstr "1\. 申请一个节点"
```

**In `index.rst`, inline markup that touches a CJK character does not parse.**
reStructuredText requires whitespace or specific punctuation before an opening
`*`, and a Chinese character is neither, so `请求的*模型 id*` renders literal
asterisks. Separate them with an escaped space — written `\\ ` in the catalog,
which is a backslash-space in the string:

```po
msgstr "客户端请求的\\ *模型 id*\\ ，与真正服务它的\\ *端点*\\ 是解耦的。"
```

This applies to `index.rst` only. Markdown pages need no escaping: CommonMark
treats a CJK character as neither whitespace nor punctuation, so `**模型 id**`
between Chinese characters is a valid emphasis run.

**One Markdown case does break, though**: a closing `**` preceded by a CJK full
stop and followed by a CJK character is not right-flanking, so
`**术语。**后文` leaves literal asterisks. Put the period outside the bold —
`**术语**。后文` — which is better typography anyway, since bolding punctuation
is wrong.

**A stale `.mo` masks your edits.** Sphinx skips recompilation when the `.mo` is
newer than its `.po`, so you can verify a build that never read your changes.
Before any verification build:

```bash
find docs/developer/locale -name '*.mo' -delete
```

The check that catches all four at once is a structural diff against the English
build: for each page, compare the counts of `<code>`, `<strong>`, `<em>` and
`<a>`, and the multiset of inline-code literals and link targets. A dropped
marker or a translated link target shows up there and in no other check.

One more, which at least fails loudly: `make docs-translate` can append
`python-format` to an entry you had annotated `no-python-format` — a source
string containing something like `≥ 5%` looks like a format string to gettext —
and `msgfmt -c` then rejects the contradictory pair. Delete the added
`python-format` and keep `no-python-format`.

## How a translation reaches the published site

`make docs` builds every published language from one `sphinx-build`. The first
language named in `DOCS_LANGUAGES` (declared in `conf.py`, defaulting to
`en:English,zh_CN:简体中文`) is the root language and lands at the top of the
output tree; each other language is written to `docs/build/html/<code>/` by a
`build-finished` hook in `conf.py`.

One invocation rather than one per language, because the published site is
built by a command that lives in the hosting project's settings, not in this
repository — there is no env var to set at publish time, so the language list
has to travel in `conf.py`. Keeping the root language at the tree root is what
preserves every URL the English-only site had: `/routing.html` stays
`/routing.html`, and the translation is at `/zh_CN/routing.html`.

The sidebar switcher links to the same page in each other language and renders
nothing when fewer than two languages are declared, so a single-language site
never shows a dead control. `DOCS_LANGUAGES="en:English"` builds English alone
when a translation is not what you are working on.

## The check that catches a reverted translation

```bash
make docs-verify
```

`make docs` plus `ops/ci/check_docs_translations.py`, and the same thing the
Docs Build CI job runs. It exists because none of the failures above are
visible to `-W`: Sphinx falls back to the English source for any string it
cannot translate, so a page that has quietly reverted still builds clean. Five
checks —

- **fuzzy** entries, which Sphinx refuses to apply;
- **missing catalogs**, for a page added without `make docs-translate`;
- **stale catalogs** — an English edit whose `msgid` no longer matches any
  entry, which is the commonest case and carries no `fuzzy` marker at all
  because nothing re-merged;
- **block starts** — a translation that begins with `1. `, `- `, `# ` or `> `,
  which re-parses as a list or heading and is dropped;
- **structural divergence** between the built pages, which is what catches the
  CJK-adjacent markup traps above.

## Chinese terminology

Use the same Chinese for an English term on every page, and define a new term
in the [Glossary](glossary.md) first. Terms that stay in English are written in
English in the Chinese text too.

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
| provider, kind, adapter, router, overlay, manifest | provider、kind、adapter、router、overlay、manifest |
