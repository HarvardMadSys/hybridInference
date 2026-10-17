# 用户控制台多语言切换设计（中文 / 英文）

- 日期：2026-10-17
- 状态：已接受。§10 中的产品决定已确认；P0 与账户同步已在首个实现 PR 中落地（§8）
- 范围：`apps/frontend/` 的 user console，以及后端 `users.preferences` 中的一个字段
- 相关记录：
  [Site UI 实现记录](2026-09-22-site-ui-implementation.md)、
  [Distribution customization](../../developer/distribution-customization.md)、
  [文档翻译流程](../../developer/translations.md)

## 1. 背景与现状

控制台目前只有一种语言，即英文。代码中有一层很薄的翻译层，但它是为
“部署方整体换文案”设计的，不支持用户自己切换语言：

| 现有机制 | 位置 | 能做到什么 | 做不到什么 |
|---|---|---|---|
| `translate(slot, fallback)` | `src/lib/i18n/translate.ts` | 原样返回调用处传入的英文 | 没有任何字典 |
| `useT()` + `translator(messages)` | `src/components/providers/useT.ts` | 用编译进镜像的 site-ui 模块的 `authMessages` 覆盖 `AUTH_MESSAGE_KEYS` 声明过的键 | 只能覆盖公开页和账户页；整个部署只有一套文案 |
| 模块 server `locale` | `src/site-ui/contract.ts`、`SiteDocument.tsx` | 设定模块所渲染路由的 `<html lang>` | 其余路由固定为 `en` |
| `fill()` | `src/lib/utils/interpolate.ts` | `{name}` 占位符替换 | — |

另有两点相关：

- 调用 `useT` 的文件约 11 个，集中在首页、登录、注册、找回密码、
  terms、authorize 和 chat。dashboard（`src/app/dashboard/` 约 1.4 万行）、
  admin（`src/components/features/admin/` 约 6.7 千行）以及 settings
  页面的文案都直接写在 JSX 和 zod schema 里。
- 后端 `users.preferences` 是一个 JSONB 列，已经有
  `get_user_preferences` / `update_user_preferences`，`llm_prober_layout`
  就存在这里，并通过 `/preferences/llm-prober-layout` 读写。用户的语言偏好
  可以直接复用这个列，不需要做数据库迁移。

## 2. 目标与非目标

**目标**

1. 用户可以在中文和英文（`en`）之间切换。中文只提供一种，不区分简体和繁体，
   内部标记为 `zh-CN`，所有 `zh-*` 浏览器语言都归入它。
2. 用户从未选过语言时，按浏览器（系统）语言自动选择；选过之后，始终显示用户
   **最近一次选择**的语言，刷新、重新登录、换设备后都保持。
3. 某个字符串没有翻译时回退到英文，页面不会显示空白或裸 key。
   这与文档翻译流程中“部分翻译是安全的”原则一致。
4. 英文原文修改后，对应的中文译文能被自动标记为过期，而不是悄悄与原文脱节。
5. 不破坏现有的 site-ui 模块：v1 模块不改代码也能继续构建和运行。

**非目标（本期不做）**

- URL 前缀式路由（`/zh/dashboard`），原因见 §4.1。
- 翻译后端 API 返回的错误信息、邮件模板、管理员撰写的广播或站点更新等
  Markdown 内容，后续阶段见 §8。
- **只给运维和内部人员用的页面**：Admin 面板（`/dashboard/admin` 和
  `src/components/features/admin/`）、首次运行设置（`/setup`）、配置页，以及只有
  internal 账户能打开的 API 调试台（playground）。它们不面向普通用户，始终保持
  英文，也不接入 `t()`。
- 繁体中文，以及 `en`、`zh-CN` 之外的其他语言。设计上不限定语言数量，
  但本期只交付这两种。

## 3. 用户体验

- **切换入口**：
  1. 顶栏 `Header` 右侧放一个紧凑的语言菜单（`EN / 中文`），登录前后都能看到，
     公开页也能用；
  2. `/dashboard/settings` 中加一项“界面语言”。
- **切换行为**：点击后立即生效，不跳转、不整页刷新，当前表单输入不会丢失
  （见 §4.4）。
- **语言名称用该语言自身书写**：选项固定显示为 “English” 和 “中文”，
  不随当前界面语言翻译，避免用户切错之后找不到切回来的入口。
- **切换入口的形态**：顶栏为每个“非当前语言”显示一个按钮，两种语言时就是一个
  “中文”或“English”按钮；settings 中是单选组。

## 4. 设计

### 4.1 语言放在 cookie 中，不用 URL 前缀

当前语言由 cookie `hi_locale` 决定，URL 保持不变。

不采用 `/zh/...` 前缀的理由：

- 控制台的主体是登录后的应用，按语言区分 URL 带来的 SEO 收益很小；
- URL 前缀会影响一批依赖固定路径的地方，包括 public path table、反向代理
  规则、`/agents` 代理、OAuth `/authorize` 回调、邮件中的链接，以及 site-ui
  模块按路由决定“由谁渲染”的判断（`useModuleRendersRoute`）；
- 根布局已经是 `force-dynamic`，在服务端读取 cookie 不会带来额外的缓存损失。

### 4.2 语言解析：首次跟随系统，之后跟随最近一次选择

**只有用户主动选择时才写入记录。** 自动检测的结果每次请求重新计算，不落盘。
因此“有记录”就等价于“用户选过”，系统语言变化只会影响从未选过语言的用户。

记录存在两个地方：

| 位置 | 内容 | 用途 |
|---|---|---|
| cookie `hi_locale` | `locale`、`chosen_at`（毫秒时间戳）、归属与状态，见下 | 每个请求在服务端直接读取，不需要后端调用 |
| `users.preferences.locale` | `{ "value": "zh-CN", "chosen_at": "<定宽 ISO 时间>" }` | 跨设备、跨浏览器保留选择 |

**每个请求**（根布局，服务端）按以下顺序确定语言：

1. cookie `hi_locale` 中的选择，前提是该值是本构建支持的语言；
2. 请求头 `Accept-Language` 与支持语言的最佳匹配（按 q 值排序，`q=0` 和 `*`
   忽略），所有 `zh`、`zh-*` 都归为 `zh-CN`；
3. `en`。

**选择的归属。** 一个浏览器可以先后登录多个账户（多账户设计 #1524），因此 cookie
中的每条选择都记录它属于谁、是否已存入：

| 编码 | 含义 |
|---|---|
| `zh-CN\|<ms>\|a` | 未登录时做出的选择，不属于任何账户，从未存入 |
| `zh-CN\|<ms>\|p\|<账户 id>` | 该账户登录时做出、尚未确认存入（请求未返回或失败） |
| `zh-CN\|<ms>\|s\|<账户 id>` | 已存入该账户 |

（初版只有一个 `pending` 标记，把“未登录的选择”和“某账户存失败的选择”混在
一起，导致 A 账户存失败的选择会在切换到 B 后被写进 B。评审中复现，已据此修订。）

**账户同步**（`planLocaleSync(browser, account, userId)`）的规则是“以最近一次
选择为准，但选择只属于做出它的账户”：

| cookie 中的记录 | 账户中的记录 | 动作 |
|---|---|---|
| 无 | 无 | 不做任何事，继续按系统语言显示 |
| 无 | 有 | 用账户记录写 cookie（归当前账户、已存入），语言不同时刷新 |
| 未登录的选择，或当前账户未存入的选择 | 无，或比 cookie 旧 | 写入当前账户（失败的重试也走这里） |
| 未登录的选择，或当前账户未存入的选择 | 比 cookie 新 | 采用账户记录 |
| 当前账户已存入的选择 | 与之相同，或比 cookie 旧 | 不做任何事（较旧的是过时的 profile 副本，见下） |
| 当前账户已存入的选择 | 比 cookie 新 | 采用账户记录 |
| 其他账户的选择（无论是否存入） | 有 | 采用账户记录；**绝不**写入当前账户 |
| 其他账户的选择 | 无 | 不做任何事（浏览器继续显示它，但不写入） |

**写回保护。** 无论是切换语言（`useChooseLocale`）还是同步（`LocaleAccountSync`），
请求返回后只在两个条件同时成立时才写回 cookie：当前登录的仍是发请求时的账户；
cookie 中仍是发出的那一条选择（四个字段完全相同，`replaceBrowserChoiceIf`）。
否则说明期间已切换账户并采用了新账户的选择，或用户又做了新的选择，旧响应被丢弃。

**过时的 profile 副本。** 页面手里的账户记录是某次加载 `/user/me` 时读到的，可能
早于当前账户之后存入的选择：本标签页刚存入，profile 还没重新加载；或另一个标签页
存入了新选择。账户一定持有它存入的选择或更新的选择，所以比 cookie 中“当前账户已
存入的选择”更旧的账户记录只能是过时副本，不能据此把语言改回去。存入成功后会重新
加载 profile；在其他浏览器做出的更新选择，会在下一次加载 `/user/me` 时被采用。
（初版在窗口重新获得焦点时用过时副本把中文改回了英文，评审中复现，已据此修订。）
同步发现无事可做时，若 cookie 中的语言与页面当前显示的不同（另一个标签页刚切换过），
刷新页面以显示它。

**重试。** 同步在以下时机重新执行：每次加载 profile（`refreshUser` 产生新的 user
对象，即使字段没变）、浏览器恢复联网（`online`）、窗口重新获得焦点（`focus`）。
计划是幂等的，两边一致时什么都不做；同一条选择同时只有一个请求在途。

**服务端“最新者胜”。** `PUT` 的比较与写入是同一条 `UPDATE ... WHERE` 语句，在
数据库中执行（§6）。并发时较旧的请求即使后落库，也会在行锁释放后重新检查条件
而放弃写入，返回已存的更新记录，客户端随之显示返回的语言。

**导航后的补刷新**：登录成功后同步逻辑会写 cookie 并调用 `router.refresh()`，
但登录页紧接着的 `router.push('/dashboard')` 会丢掉这次刷新，而页面间的客户端
导航不会重新渲染根布局。因此每次路径变化后，若页面语言与 cookie 中的选择不一致，
再刷新一次。这个问题是浏览器端到端验证时发现的。

整个过程只依赖已有的 `/me` 请求，不会给 SSR 增加后端调用。

### 4.3 字符串 API：沿用 `t(key, englishFallback)`

继续使用现有的调用形式，不改签名：

```ts
const t = useT();
t('dashboard.keys.title', 'API keys');
fill(t('dashboard.usage.tokens_today', '{count} tokens today'), { count: fmt.number(n) });
```

- **英文写在调用处，作为唯一的源文本**。不另外维护 `en.json`：英文一改，
  代码评审时就能看到，读代码的人也不必跳到字典里去看页面上显示的是什么。
  这也是现有 `useT` 已经确立的约定。
- **插值沿用现有的 `fill()`**，不给 `t` 加第三个参数。复数形式先不引入 ICU：
  中文没有单复数之分，英文侧需要复数的地方很少，用两个 key
  （`*_one` / `*_other`）配合 `Intl.PluralRules` 选择即可。如果以后要支持
  复数规则复杂的语言，再引入 `intl-messageformat`。
- **服务端组件**（`generateMetadata`，以及 `/terms`、`/team` 这类服务端页面）
  使用同一个 `catalogTranslator()`，字典由服务端按语言加载（尚未接入，随 P1 做）。`translate.ts` 注释中
  记录过 client/server 边界上的那次故障，因此这一层继续保持不带
  `'use client'`。
- **key 命名**：`<区域>.<页面或组件>.<含义>`，与现有的
  `auth.consent.age_body` 保持一致。
- **同一个 key 对应多句英文**：现有代码中有少数 key 在不同位置（或通过
  `cond ? 'a' : 'b'`）传入不同的英文，这是模块契约里共用一个可覆盖 key 的结果。
  字典中这样的 key 存一个条目列表，每句英文一条。

不采用 next-intl：现有的 `translator` / `useT`、site-ui 的 `authMessages`
覆盖链和服务端边界都已经建立，引入 next-intl 等于再建第二套系统，并要重写
上面这 11 个调用点和模块覆盖逻辑。我们实际需要的只有“按语言选字典、插值、
格式化”，在现有层上补齐就够了。

### 4.4 字典、加载与运行时

```text
apps/frontend/src/lib/i18n/
  locales.ts           # SUPPORTED_LOCALES、语言名称、Accept-Language 匹配
  choice.ts            # cookie 编解码、请求语言解析、planLocaleSync
  browserChoice.ts     # 浏览器端读写 cookie
  request.server.ts    # 服务端：requestLocale()、catalogFor()
  translate.ts         # catalogTranslator()
  messages/zh-CN.json  # { "<key>": { "source": "<英文原文>", "text": "<中文>" } }
apps/frontend/src/components/providers/
  LocaleProvider.tsx   # locale、字典、模块语言的 context
  localeChoice.ts      # useChooseLocale()
  LocaleAccountSync.tsx# 账户同步与导航后补刷新
apps/frontend/scripts/i18n/check-catalogs.mjs   # 字典检查（§7）
```

`useFormatter()`（§4.5）随 P1 实现。

- **字典内容**：`zh-CN.json` 的每一项同时存英文原文 `source` 和译文 `text`。
  运行时只有当 `source` 与调用处传入的英文完全一致时才使用 `text`，否则回退
  英文。这样英文一改，旧译文自动失效，不会出现“英文已经改了，中文还在说旧的
  意思”。原理与文档的 gettext `fuzzy` 机制相同（`translations.md`）。
  若担心包体积，构建时可以把 `source` 压成短哈希。
- **加载**：根布局在服务端确定语言后，只把这一种语言的字典作为 prop 交给
  `LocaleProvider`，`en` 不需要字典。字典打包进镜像，不经过 `/site-config`
  下发，因为 `useT` 注释中已经说明，文案必须随镜像一起走版本锁。
- **体积**：admin 不翻译，预计中文字典约 50–150 KB（未压缩），使用单个文件即可。
- **Admin 页面**：页面外框（Header、Footer、导航）跟随当前语言，admin 的
  正文保持英文。admin 内容容器标注 `lang="en"`，让屏幕阅读器和浏览器翻译
  正确识别这部分语言。
- **切换**：`setLocale(next)` 依次执行：
  1. 写 cookie `hi_locale`（`locale`、当前时间 `chosen_at`、归属：未登录为
     `a`，登录时为 `p|<账户 id>`；`Path=/; Max-Age=1y; SameSite=Lax`）；
  2. `router.refresh()`，让服务端组件、`<html lang>` 和 metadata 用新语言
     重新渲染，client 状态保留；
  3. 已登录时调用 `PUT /user/preferences/locale`，由服务端记录 `chosen_at`，
     成功且通过写回保护（§4.2）后把 cookie 标为已存入。失败时不提示、不回滚
     界面，cookie 保持“该账户未存入”，由同步的重试时机再写入。
- **`<html lang>`**：`SiteDocument` 的判断改为
  `moduleRenders && moduleLocale ? moduleLocale : activeLocale`（`documentLocale()`），
  `useT` 用同一个函数决定页面语言，见 §5。“当前路由是否由模块渲染”由
  `SiteUiBoundary` 通过 context 传给 `useT`，`useT` 不再自己查路由。
- **水合一致**：locale 只在服务端解析一次，通过 props 传到 client，client
  不再自行读取 cookie 或 `navigator.language`，避免服务端和客户端渲染结果不一致。

### 4.5 格式化

目前有 37 个文件直接调用 `toLocaleString` / `toLocaleDateString` /
`Intl.*`，其中一部分写死了 `'en-US'`。改为通过 `useFormatter()` 统一处理：

```ts
const fmt = useFormatter(); // 内部绑定 activeLocale
fmt.number(n); fmt.date(d, 'short'); fmt.relative(d); fmt.usd(cost);
```

- 金额统一显示为 USD（`$`），不随语言切换币种，只改变数字分组方式；
- 时区规则保持不变，本设计只改变日期的书写形式；
- 图表的坐标轴和 tooltip 也使用同一个 formatter。

### 4.6 表单校验文案

zod schema 现在定义在模块顶层，例如 settings 页面的 `changePasswordSchema`，
校验文案在模块加载时就固定成了英文。改为在组件内用工厂函数生成：

```ts
const makeChangePasswordSchema = (t: Translate) => z.object({ ... t('settings.password.min_length', 'Password must be at least 8 characters') ... });
const schema = useMemo(() => makeChangePasswordSchema(t), [t]);
```

规则本身不变，只把文案换成 `t()`。这与 `useT` 注释中“规则自带文案，不从
字典读取规则”的约定一致。

### 4.7 字体与排版

- `Crimson_Text` 只覆盖拉丁字符，中文会落到浏览器默认字体。在 `globals.css`
  的字体栈中显式加入
  `"PingFang SC", "Hiragino Sans GB", "Noto Sans SC", "Microsoft YaHei"`，
  不额外下载中文 webfont，因为体积太大。
- 中文通常比英文短，但按钮、表头、徽章在一两个字时可能显得过窄。验收时
  需要在中文下截图检查（见 §8）。

## 5. 与 site-ui 模块的关系

模块是部署方编译进镜像的 UI，可以接管 `/`、账户页和 `/terms`，它的文案
不归控制台所有。

**已实现的规则（v1 模块零改动）**：每个页面先确定“页面语言”
（`documentLocale()`：模块以固定 `locale` 渲染的路由用模块语言，其余用读者的
语言），然后：

1. 模块的 `authMessages` 用模块语言写成（descriptor `locale`，为空时视为英文），
   只在公开路由、且页面语言与之相同时生效。控制台路由一律不用（保持原规则）。
2. 控制台字典只在页面语言等于读者语言时生效。
3. 优先级：模块文案 → 控制台字典 → 调用处的英文。

由此：模块 `locale` 为空（对英文的改写）时，英文读者看到模块的措辞，选了中文
的读者看到控制台的中文；模块声明固定语言（例如 `fr`）且渲染该路由时，该路由
仍是模块语言，与读者选择无关。一个行为变化需要注意：声明了固定语言、但**不**
渲染账户页的模块，过去它的 `authMessages` 会出现在控制台渲染的账户页上；现在
只有当读者语言与模块语言相同时才出现。

**推迟到后续的契约扩展**（向后兼容，不提升 `siteUiApi`）：

- client：`authMessagesByLocale?: Partial<Record<Locale, AuthMessages>>`；
- server：`locales?: readonly string[]` 与 `metaMessagesByLocale`；
- host facade 导出 `useLocale()` / `useFormatter()` / 语言切换组件，让模块的
  `Landing` 和 `AuthFrame` 能随读者语言渲染并放置切换入口。模块接管的页面
  不显示控制台顶栏，因此在此扩展之前，这些页面上没有切换入口。

## 6. 后端改动（已实现）

只增加一个用户偏好字段，不做数据库迁移：

- `preferences.locale`：`{ "value": "en" | "zh-CN", "chosen_at": "<ISO 时间>" }`，
  或不存在（表示从未选择过）。`chosen_at` 以定宽格式写入
  （`2026-10-17T08:00:00.000000Z`）。无法识别的值按“从未选择”处理；
- `PUT /user/preferences/locale`，请求体为 `{ "locale": "zh-CN", "chosen_at": "<可选>" }`，
  `locale` 按白名单（`ConsoleLocale`）校验。`chosen_at` 只在把未存入的选择写入
  账户时由客户端传入，服务端会把它截断到不晚于当前时间；其他情况取当前时间；
- `GET /user/me` 的响应中增加 `locale`（可为空）。

**偏好按 key 原子写入。** 初版沿用 `llm-prober-layout` 的“读取整列 → 修改 →
整列写回”，评审复现了两类丢失：较旧的语言选择后落库覆盖较新的；并发修改的
`llm_prober_layout` 被整列写回抹掉。修订后：

- 存储接口去掉 `update_user_preferences`（整列替换），改为
  `set_user_preference` / `delete_user_preference`（`jsonb_set` 与 `-`，只动一个
  key），以及 `set_user_preference_if_newer`：比较与写入在同一条
  `UPDATE ... WHERE NOT (stored.chosen_at >= new.chosen_at)` 中完成，行被并发
  更新锁住时，Postgres 会在锁释放后对最新版本重新检查条件；
- `llm_prober_layout` 的 PUT/DELETE 和管理员设置 `disabled_models` 都改用按 key
  写入，任何偏好写入都不再覆盖其他 key；
- 存储的 `chosen_at` 先用正则确认是 ISO 时间再转换为 `timestamptz`（放在 `CASE`
  中，保证不合法的值不会触发转换错误），不合法的值视为“没有选择”。

**未实现**：运行时 site-config 中的 `i18n: { default_locale, enabled_locales }`
开关。目前所有部署都同时提供两种语言；如果需要按部署关闭中文，再单独加这个开关
（它只是开关，不含文案，不影响“文案随镜像”原则）。

## 7. 工具与质量门（已实现）

- `npm run i18n:check`（`scripts/i18n/check-catalogs.mjs`）用 TypeScript
  编译器扫描源码中的 `t(...)` / `translate(...)` 调用，按（key, 英文）对报告
  已翻译、缺失、过期、孤儿条目。`--todo zh-CN` 输出待翻译清单，`--prune`
  删除孤儿条目。
- 硬错误：key 是字面量但英文不是由字面量构成；字典条目格式错误；译文的
  `{占位符}` 与原文不一致。
- `src/lib/i18n/catalog.test.ts` 在 `npm run test` 中对随镜像发布的字典执行
  同样的检查，并要求没有过期和孤儿条目，因此 CI 无需新增步骤。
- 开发者文档：`docs/developer/console-translations.md`（写法、字典、检查、
  保留英文的内容、术语表）。

## 8. 分期

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 基础设施 | §4.1–4.4、Header 切换入口、settings 语言选项、字体栈、检查工具；翻译 Header、Footer、账户菜单 | 首个 PR 完成 |
| P2 账户同步 | §6 后端偏好与 `/user/me`；§4.2 的同步规则与导航后补刷新 | 首个 PR 完成 |
| P1a 已有 key 的页面 | 首页、登录/注册/找回密码/重置密码/邮箱验证、authorize、chat、team：代码中已有的 216 条（key, 英文）对全部补中文 | 首个 PR 完成 |
| P1b 其余用户页面 | dashboard 首页（API key、用量、模型、最近请求）、settings 其余部分、删除账户、首页各区块、`/stats`、错误页与提示；§4.5 格式化（`useFormatter`、`useStatsFormat`），§4.6 校验文案；服务端页面（team、stats 标题）通过 `requestTranslator()` 按请求语言翻译 | 同一 PR 完成 |
| 法律与知情同意文本 | 服务条款与注册时的研究知情同意条款，按 §10 第 4 条直接翻译 | 同一 PR 完成 |
| 模块契约扩展 | §5 的推迟项 | 待做 |
| 后续 | 后端错误码化（`detail` 加上稳定的 `code`，前端按 code 翻译）；按用户语言发送邮件 | 单独设计 |

合并后，普通用户能看到的控制台页面全部提供中文：字典覆盖代码中全部 681 条
（key, 英文）对。仍为英文的只有后端返回的错误信息、管理员撰写的内容、模型名称，
以及 §2 列出的运维和内部页面。

## 9. 风险与注意事项

- **HTML 缓存**：如果 CDN 或反向代理缓存了公开页的 HTML，按 cookie 切换语言
  会串页。需要确认当前部署不缓存 HTML，或者给响应加上 `Vary: Cookie`。
- **测试**：现有测试断言的是英文文本。没有 `LocaleProvider` 时默认是英文，
  这些测试不受影响。
- **混合语言**：后端错误、管理员撰写的 Markdown、模型名称仍是英文，中文界面
  中会夹杂英文。这是有意的，因为这些内容不归前端所有。
- **法律与知情同意文本**：服务条款（`terms.*`）和注册时的研究知情同意条款
  （年龄、研究参与、数据共享、同意条款）有法律或研究伦理效力。按 §10 第 4 条，
  它们直接翻译，中文版不附“以英文为准”的说明。因此英文一旦修改，必须在同一个
  变更里更新译文；字典测试会拒绝过期条目。如果研究涉及伦理审查（IRB），中文版
  可能需要报审查方批准。
- **翻译质量**：译文由实现者逐条写成，力求口语化、贴近中文用户的习惯；仍建议
  团队中文母语者审校术语表和主路径。

## 10. 已确认的决定（2026-10-17）

1. Admin 面板不翻译，因为它不面向普通用户（§2）。
2. 中文只提供一种，不区分简体和繁体，所有 `zh-*` 都归入 `zh-CN`（§2、§4.2）。
3. 首次访问时按系统语言自动选择，之后显示用户最近一次选择的语言（§4.2）。
4. 服务条款和研究知情同意文本直接翻译成中文，不附“仅供参考、以英文为准”的
   说明（§9）。

## 11. 待决问题

1. 外部 distribution 模块（例如 freeInference）是否需要随 P2 一起提供中文版
   landing？
