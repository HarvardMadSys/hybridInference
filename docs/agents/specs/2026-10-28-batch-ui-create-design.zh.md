# 控制台创建 Batch 任务：产品与技术设计

- 日期：2026-10-28（Asia/Shanghai）
- 编写与审阅：Claude Code（Opus 5.5 / high）起草，Codex 对照代码审阅并补充边界条件。
- 状态：**产品范围已确认，已实现（未合并）。** 最新确认：上传后自动识别类型，只有信息不足时才补充选择。实现中按审阅意见修订了 I1、I2、I6、I7、I8 与提交顺序（见第 10 节）；验收记录见 [docs/reviews/2026-10-28-batch-ui-create.md](../../reviews/2026-10-28-batch-ui-create.md)。
- 原调研基线：`dev` `5df326620b8dcc834f4fb8648f6dffa6dd708d7b`。设计初稿以 [PR #1687](https://github.com/HarvardMadSys/hybridInference/pull/1687) 的统一 Batch 合同为基础；交付前已核对并在隔离实现分支合入其最新 head `f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b` 及 `dev` `453884a396a60f6df3a5dd04c73f8c1845e672e1`。#1687 与入口权限 #1680 仍为 OPEN、未合并；本地整合和测试不代表已上线。
- 前置依赖：[PR #1680](https://github.com/HarvardMadSys/hybridInference/pull/1680)（Batch 入口与权限第一阶段，head `aba29b25e611096ffaff28d034a7aade95939f9c`）。交付本文时，Codex 已独立确认该 PR 为 OPEN 状态，**不在上述基线中、尚未合并**；本轮未核实部署状态。本文沿用它已批准的规则，不另行设计。
- 本次修订：统一任务类型、输入方式、状态、计数和结果格式；删去按 OpenAI / Anthropic 分支展示的旧设计。首版创建范围仍然是 JSONL + Chat Completions / Embeddings，Messages / Responses 的网页创建与复制仍暂缓。
- 读者：算法、数据与评测工程师，以及产品经理。第 1–3 节讲产品，第 4 节起讲技术。
- 相关记录：[批处理设计](2026-10-25-batch-inference-design.md)、[API 扩展](2026-10-26-batch-api-expansion-design.zh.md)、[默认可用性修订](2026-10-27-batch-default-availability-amendment.zh.md)、[batches.md](../../developer/batches.md)。

| 术语 | 含义 |
|---|---|
| 源文件 F | 用户上传的 JSONL。存储后字节不再改变 |
| 覆盖 O | 第 ② 步明确设置的字段值，统一写入每一行；没有设置的字段保留文件原值 |
| 最终请求 | F 的每一行先应用 O，再把模型固定为确认过的规范 ID，得到的就是存入任务的请求。适配器向上游发送前还会做协议转换，例如格式翻译和默认值处理；这一步不属于「最终请求」 |
| 预检 | 服务端对整份文件做只读校验。不调用模型、不计费，也不保证每条请求都能执行成功 |
| 运行凭据 | worker 每次执行请求前都要重新校验的身份。现有实现中是 API 密钥（见 4.2） |
| 提交意图 | 用户在第 ③ 步点击一次「确认提交」所对应的那一份内容。它有一个幂等键，所有重试和恢复都共用这个键 |

## 1. 一页摘要（给产品经理）

**目标**：在 **Tools → Batches** 加入「创建任务」。已登录用户不写代码、不粘贴 API 密钥，就能把 JSONL 提交为批处理任务，并在同一处管理、复制任务和下载结果。以 #1687 为基础，整个产品只有一个 Batch 资源；`/v1/batches` 提供程序化操作，`/user/batches` 是同一资源的登录用户入口，文件仍由文件服务管理。停用旧 `/v1/messages/batches` 等接口并提供 HTTP 410 迁移提示由 #1687 负责，本功能不恢复旧接口的创建能力，也不额外改变其合并后的公共 API 合同。

**用户看到的分类**：主要显示「任务类型 + 模型」，次要显示「JSONL 文件 / 直接提交」和来源（网页 / API）。不设置 OpenAI Batch、Anthropic Batch 两类产品，也不暴露 `api_surface` 作为选择项。管理页覆盖 Chat、Embeddings、Messages、Responses 以及兼容的历史任务；首版创建页仅开放已批准的前两类。Messages 是请求格式，不等于某个厂商的全部模型；能经 Chat Completions 调用的模型，按该接口的模型目录和能力校验提供。

**已批准的首版范围**
- 输入只接受 JSONL；任务类型为 Chat Completions（文本生成）和 Embeddings。
- 每个控制台任务只用一个模型、一种任务类型。
- 复制任务：用仍然保留的源文件和原来的设置重新打开创建页，可以修改模型和参数；只有用户明确点击提交才会生成新任务，复制这一步不执行任何请求。源文件缺失、被删除或已过期时，要求重新上传。
  **首版能复制的任务**：控制台创建的任务，以及输入文件仍在、`target` 为 Chat/Embeddings 的 API 文件任务。**直接提交的任务**没有输入文件，复制按钮置灰并说明原因；Messages / Responses 类型暂不支持网页复制。判断依据是任务类型、输入方式及源文件状态，不是厂商或历史 `api_surface` 名称。
- 「导出未成功请求」：把失败、过期未执行、已取消条目的原始请求导出，人工修改后重新上传。这是手动导出，首版不提供一键重试。
- 第 ③ 步预览的就是实际存入任务的请求。文件里包含网关不会转发的字段时，预检直接报错，不会出现「看起来提交了、实际被忽略」的值。现有 `/v1` API 的行为不变。

**用户故事**
1. 评测工程师打开上周用模型 A 跑完的 2,000 条评测，点「复制为新任务」，把模型改成 B，确认后提交，得到一个新任务。
2. 文件里每行的 `temperature` 各不相同。这次把它统一设为 0，预览显示 2,000 行都会被改写，其余字段不变。
3. 上传 embeddings 文件、选向量模型。页面不出现 temperature 这类文本参数。
4. 文件第 17 行（custom_id `q-17`）缺少 `messages`，第 230 行的 custom_id 与第 12 行重复。整批无法提交；用户下载错误清单，改好后重新上传。
5. 点击提交后网络超时。重新打开页面后，系统用同一个提交意图确认任务已经创建，不会因为重试多建一个任务。注意：这只防止重复建任务，单条请求仍可能因为 worker 重试或费用未知的尝试而被多次计费，这是现有行为。
6. 文件里写的模型已经下线。用户在第 ② 步把模型统一换成可用的模型，然后通过校验。

**三步流程**

```text
Batches 列表 ─[创建任务]─▶ ① 上传数据 ─▶ ② 配置任务 ─▶ ③ 校验与确认 ─[提交]─▶ 任务详情
                           示例/格式说明     名称/描述        预检结果/错误清单
                           大小/行数/限额    模型、可选覆盖   存储请求预览
                           只做结构识别      少量参数覆盖     计费与保留说明
任务详情 ─[复制为新任务]─▶ ②（带入源文件和原设置）─▶ ③ ─[提交]─▶ 新任务
```

| 首版必须有 | 推迟（不在首版，也不顺带做） |
|---|---|
| 专用创建页三步流程；示例 JSONL 与格式说明 | 样本试跑 / 测试运行；一键重试失败条目 |
| 文件大小、行数、类型和本部署限额 | CSV/Excel 导入与列映射；在线编辑任意记录 |
| 自动建议、可编辑的任务名称；可选描述 | 数据集库、版本管理、团队共享 |
| 模型选择 + 少量按端点区分的参数覆盖 | 多模型扫描与对比看板 |
| 预检报告行号、custom_id 和原因；有错误时整批阻止；可下载错误清单 | 精确费用估算 / 预计完成时间（ETA） |
| 存储请求预览（截断时标明）；提交确认；同一提交意图重试时不会重复建任务 | 独立的公共 system prompt 编辑器 |
| 列表：名称搜索、状态筛选、成功/失败/总数、暂停原因、已知/未知费用、结果到期时间 | 自选执行窗口（文件批次固定 24 小时） |
| 详情：配置来源、取消语义、统一结果/错误下载、导出未成功请求、复制 | Responses / Messages 的控制台创建（统一 `/v1/batches` API 支持） |
| 必要的最小清理入口：列出并删除自己上传的文件，只在空间不足时使用，不是数据集管理 | 密钥页「撤销会影响 N 个任务」的提醒（可选增强） |

实施采用第 8 节已说明的默认方案：绑定现有有效 API Key；复制仅限 Chat/Embeddings 文件任务。

## 2. 页面与交互（低保真）

**入口**：沿用 PR #1680。统一的 Tools 区块按 `show_entry` 决定是否显示 Batches 链接；「创建任务」和「复制为新任务」只在 `role_may_submit` 为真时显示。角色低于要求、但有历史任务的用户，仍可以查看、下载、取消、删除自己的任务，页面会说明他们不能提交新任务。不新增角色、设置或权限界面。前端隐藏按钮只是提示，后端仍会逐次检查。

### ① 上传数据

```text
┌ 创建批处理任务   ①上传数据 ─ ②配置任务 ─ ③校验与确认 ─────────────────────────┐
│ [拖入或选择 .jsonl]   [下载示例：Chat] [下载示例：Embeddings] [格式说明 ▸]    │
│ 本部署限额：≤32 MiB · 单行 ≤4 MiB · ≤50,000 行 · 每小时上传 ≤60 次            │
│            文件空间 41/512 MiB · 未结束任务 2/10        （数值读自当前设置）  │
│ requests.jsonl · 12.3 MiB · 2,000 行 · SHA-256 3f9a…   [更换文件] [下一步 →] │
│ 识别：Chat 2,000 行 · 模型 old-model 1,200 行（⚠当前不可用）/ model-y 800 行   │
└──────────────────────────────────────────────────────────────────────────────┘
```

浏览器只读取文件名和大小，不解析内容。上传显示进度，可以取消。第 ① 步的「识别」只检查结构，并统计任务类型、模型和参数的分布；**模型不可用不会在这一步拦截**，只用 ⚠ 标出，留到第 ② 步决定是否统一替换。最终准入在第 ③ 步完成。

### ② 配置任务

```text
┌ ②配置任务 ───────────────────────────────────────────────────────────────────┐
│ 名称* [requests-model-z-20261028-1430]   描述 [可选]                          │
│ 已识别：文本生成 · 2,000 条                   ← 只读信息，无必选类型控件     │
│ 模型 ( )使用文件中的模型   (●)统一改为 [model-z ▾]  ← 混合或不可用时只能统一改 │
│ temperature (●)保留文件值 0:800 · 0.7:1,100 · 未设置:100  ( )统一设为 [0]      │
│ max_tokens  (●)保留文件值 512:2,000                       ( )统一设为 [1024]   │
│ 文件中的其他参数（原样保留，这里不能改）：top_p 300 行 · stop 20 行            │
│                                           [← 上一步] [校验并预览 →]          │
└──────────────────────────────────────────────────────────────────────────────┘
```

Embeddings 只显示模型选项。模型下拉框列出当前用户可见、类型匹配的非 auto 模型；能否使用以第 ③ 步的准入为准。

**类型自动识别（最新确认，优先于旧草图中的类型选择）**：
- 正常路径只有「上传文件 → 确认模型和配置 → 检查并提交」，不显示必选的 Chat/Embeddings 单选框，也不设两个创建入口。
- 服务端逐行识别：显式 `url` 是首要依据；缺少 url 时，只对能唯一识别的请求结构推断（例如 messages 对应 Chat，input 对应 Embeddings）。完整文件必须一致，不能只看首行或依赖模型名称猜测。
- 类型确定后显示只读标签，并过滤兼容模型、显示适用参数。确认页明确写「已识别：文本生成/文本向量化，共 N 条」。
- 只有合法 JSON 缺少足够信息、仍有歧义时，才展示补充类型控件。该选择只补齐缺失的信息；不能覆盖显式 url、吞掉非法字段，或把一种请求改造成另一种。无法通过所选 schema 的行仍报错。
- 混合类型、显式 url 与请求结构冲突、不支持的 endpoint 均按行报错。混合文件提示拆分；不能借手动选择让整份文件通过。
- 文件中已有一个可用模型时默认沿用；模型覆盖始终是用户明确选择。同类模型才能替换，选择模型不能隐式转换请求类型。


### ③ 校验与确认

```text
┌ ③校验与确认 ─────────────────────────────────────────────────────────────────┐
│ ✗ 3 个错误，整批不能提交                         [下载错误清单 CSV/JSONL]     │
│  行17  q-17  body.messages  缺少 messages                                     │
│  行230 q-12  custom_id      与第 12 行重复                                    │
│ —— 全部通过后显示 ——                                                          │
│  行40  q-40  body.max_completion_tokens  网关不会转发此字段；请在文件中改为   │
│              max_tokens（不会自动转换）                                       │
│ —— 全部通过后显示 ——                                                          │
│ ✓ 2,000 行格式有效（不计费；不保证每条请求都能执行成功）                       │
│ 存储的请求预览（前 3 行；超长内容标注「已截断」）                  [展开 JSON] │
│ 确认：名称 · Chat · 模型 model-z（规范 ID model-z-v2，按此固定）· 2,000 条     │
│  参数 temperature 统一为 0（原 0/0.7/未设置）；max_tokens 保留文件值           │
│  执行窗口 24 小时内开始全部请求；已开始的请求可在窗口结束后完成               │
│  计费 与实时调用同价，没有批量折扣；计入每日额度，额度用尽时暂停；不提供预估   │
│  运行凭据 API 密钥 hyi-ab12…（运行期间退出登录或关闭浏览器不受影响；撤销或     │
│          重新生成这把密钥会使未结束的任务失败）                                │
│  保留 结果在任务结束后保留 7 天；源文件保留到 11-27                           │
│                                              [← 上一步] [确认提交]           │
└──────────────────────────────────────────────────────────────────────────────┘
```

只要还有错误就不能提交，系统不会只提交其中合格的行。预览展示的是存入任务的请求；适配器发给上游前的协议转换不在预览范围内。确认页显示的规范模型 ID 和密钥标识会和这次提交绑定。如果在提交前其中任何一项发生变化（例如别名被改指到别的模型，或密钥被轮换），提交会被拒绝，并要求重新预检和确认。提交后按钮锁定；遇到超时或断网时，页面显示「正在确认提交结果」，并用**同一个提交意图的幂等键**自动重试（见 I6）。提交成功后跳转到详情页。

### 列表与详情（在现有页面上补充）

```text
┌ Batches  搜索[名称或ID]  状态[全部▾ 校验中/进行中/整理结果中/…]      [＋创建任务] ┐
│ eval-0420 · Chat · model-z                                              进行中  │
│ JSONL 文件 · 网页创建                                                        │
│ 成功1,203 · 失败12 · 过期未执行0 · 已取消0 · 未结束785 · 共2,000                 │
│ 已知费用 $3.41（另有 2 次尝试费用未知）· 结果保留到 11-04                         │
│ ⚠ 已暂停：今日额度已用尽，UTC 0 点后恢复                         [详情] [取消]   │
└──────────────────────────────────────────────────────────────────────────────────┘
详情：名称/描述/ID/创建方式 · 计数 · 已知与未知费用 · 暂停原因 · 配置来源（源文件名、
SHA-256、状态与到期时间，任务类型，模型选择与实际执行的模型，各项覆盖，复制自哪个任务）·
下载[存储的请求][成功结果][错误结果][未成功请求] · [取消][复制为新任务][删除]
取消说明：尚未开始的请求不再执行；正在执行的会完成并计费；已有结果保留，可以下载。
```

沿用 #1687 的统一合同，所有任务（含历史 `msgbatch_` 任务）都用同一套计数和状态：
- `request_counts = {total, completed, failed}`；`failed` 包含 `expired`，`cancelled` 单独统计。显示：成功 = `completed`；失败 = `failed − expired`；过期未执行 = `expired`；已取消 = `cancelled`；未结束 = `total − completed − failed − cancelled`。不再读取 Anthropic 五项计数或把旧任务统一显示为 `ended`。
- 状态枚举为 `validating / in_progress / finalizing / completed / failed / expired / cancelling / cancelled`。页面和筛选都使用后端的显示状态；文件正在封存结果时显示并可筛选 `finalizing`。新增用户列表筛选复用 #1687 的 `display_status_sql` 语义，不按内部状态另做一套规则。
- 任务类型使用 `target`，输入方式使用 `input: file | inline`；`api_surface` 只作内部兼容和诊断信息。用户列表和管理员页继续共用这套定义。

结果可用性以服务端 `results_available` 为准：文件任务等到封存后下载；直接提交及历史任务沿用 #1687 的终态规则。所有结果使用统一的 `{id, custom_id, response, error}` 外层，Messages 的原生响应保留在 `response.body`，不再使用旧的 `{custom_id, result}` 格式。统一外层不代表把 Messages 响应转换成 Chat 响应。下载按钮按输入方式提供：文件任务可分别下载成功/错误结果，直接提交及历史内联任务下载包含成功与错误的全部结果；现有接口对非文件任务会将 `kind` 归一为 `all`，不能展示两个实际下载相同内容的按钮。「错误结果」不能直接拿来重新提交；「未成功请求」导出首版仅为 Chat/Embeddings 提供可重新上传的输入，其他类型仍可下载标准结果。空列表的文案增加「[创建任务]，或通过 API 提交」。

### 复制为新任务

| 原任务 | 是否可复制 | 带入内容 |
|---|---|---|
| 控制台创建的任务，源文件仍保留 | 可以，进入第 ② 步 | 源文件（只引用，不复制字节）、任务类型、模型选择、各项覆盖、描述；名称为「原名-副本」 |
| API 创建的任务，`input=file`、`target` 为 chat/embeddings 且输入文件仍在，包括本功能上线前创建的 | **可以**，进入第 ② 步，并按控制台规则重新预检（文件中有不受支持的字段时会报错） | 把输入文件当作源文件；任务类型取 target/endpoint；模型默认使用文件中的模型；不带任何覆盖 |
| 原本符合复制类型与输入方式要求，但源文件或输入文件已删除、过期或缺失 | 进入第 ① 步，提示重新上传；其余设置照常带入 | 重新上传的文件与原文件 SHA-256 相同时，提示「与原文件内容一致」 |
| `input=inline` 的直接提交任务 | 暂不支持复制：没有可复用输入文件。按钮置灰，并说明原因 | — |
| `target=messages/responses` 的任务（包括旧 Message Batch 和新的 Messages 文件任务） | 暂不支持网页创建/复制；列表、取消、删除和标准结果下载照常提供 | — |

复制不会执行任何请求，也**不会延长**源文件的保留期。已删除或过期的源文件，不能用任务里保留的条目或「存储的请求」文件重建（见 I8）。首版采用决定 B 的文件任务范围。

### 状态与常见错误

| 场景 | 显示与处理 |
|---|---|
| 上传中 / 识别或预检中 | 显示进度或「正在检查 2,000 行…」；可以离开，返回后重新预检（预检不写任何数据） |
| 源文件在操作过程中过期或被删除 | `source_unavailable`：回到第 ①，保留其余设置 |
| 没有有效 API 密钥 | 第 ①② 步可正常使用；第 ③ 步提示「提交需要有效的 API 密钥作为运行凭据 [去创建]」 |
| 每日额度为 0 或已用尽 | 上传和预检不计费，可以照常进行；提交返回 429，提示等到 UTC 0 点（或额度调整后）再提交 |
| 未结束任务已达上限 / 文件空间已满 | 429 / 413，并给出下一步：先结束其他任务 / 打开「清理已上传文件」（只能列出和删除自己的文件，是必要的最小入口） |
| 确认后别名改指、密钥被轮换 | 409 `model_resolution_changed` / `credential_changed`：回到第 ③ 步重新预检、重新确认 |
| 角色被降级、`BATCH_ENABLED` 被关闭、登录过期 | 403 / 404 / 重新登录；草稿保存在本地，不含文件内容。已提交的任务不受登录状态影响 |
| 切换账号 | 草稿和待确认的提交按 `user_id` 分开保存，不会显示或重放其他账号的记录 |
| 提交结果未知 | 恢复同一个提交意图，见 I6；不会生成新的幂等键 |
| 结果即将到期 / 已经到期 | 提前 24 小时提醒；到期后任务被自动删除，从列表中消失 |

错误项统一包含 `line`、`custom_id`、`field`、`code` 和一句可以照着修改的 `message`，**不回显提示词内容**。主要 code：`utf8_bom`、`invalid_json_line`、`empty_line`、`duplicate_key`、`invalid_custom_id`、`duplicate_custom_id`、`invalid_method`、`mismatched_endpoint`、`unsupported_endpoint`、`unsupported_parameter`、`missing_model`、`mismatched_model`、`model_not_found`（模型不存在和无权使用不作区分，与 API 一致）、`auto_model`、`invalid_request`、`line_too_long`、`file_too_large`、`too_many_requests`、`source_unavailable`、`model_resolution_changed`、`credential_changed`、`no_worker_credential`。其余沿用现有 API 的 403/413/429 文案。

## 3. 输入格式与覆盖语义

每行一个对象：`{"custom_id","method","url","body"}`，沿用统一 Batch 文件输入的兼容格式。控制台的规则：`custom_id` 必填，长度 1–512，在文件内唯一；`method` 可以省略，填写时只能是 POST；`url` 可以省略，此时用自动识别或必要时用户补充确认的任务类型补上，填写时必须与任务类型一致；`body` 按 `/v1/chat/completions` 或 `/v1/embeddings` 的规则校验。选择了「统一改为」时，`body.model` 可以省略。空行、BOM 和同一对象内的重复键都会报错：现有的 `json.loads` 遇到重复键时会静默保留最后一个值，控制台改为明确报错。

```jsonl
{"custom_id":"q-1","method":"POST","url":"/v1/chat/completions","body":{"model":"your-model-id","messages":[{"role":"system","content":"Be concise."},{"role":"user","content":"Summarize Hamlet in two sentences."}],"temperature":0,"max_tokens":256}}
{"custom_id":"q-2","url":"/v1/chat/completions","body":{"model":"your-model-id","messages":[{"role":"user","content":"Translate \"good morning\" into French."}],"temperature":0.7}}
{"custom_id":"doc-1","method":"POST","url":"/v1/embeddings","body":{"model":"your-embedding-model","input":"The quick brown fox."}}
{"custom_id":"doc-2","url":"/v1/embeddings","body":{"model":"your-embedding-model","input":["first passage","second passage"],"dimensions":512}}
```

（示例文件按任务类型分成两份下载，上面合在一起只是为了展示。同一份文件混用两种类型会被阻止。）

| 覆盖字段 | 适用类型 | 校验 | 写入方式 |
|---|---|---|---|
| `model` | 两者 | 用户可见、类型匹配、非 auto，并通过第 ③ 步准入 | 每一行写入 `body.model`，包括原来没有 model 的行 |
| `temperature` | Chat | 0–2；模型声明了 `supported_sampling_parameters` 且其中不含 temperature 时，不提供该覆盖项 | 每一行写入，包括原来未设置的行 |
| `max_tokens` | Chat | ≥1；模型的 `max_output_length` 大于 0 时不得超过它 | 每一行写入 `max_tokens`。文件里的 `max_completion_tokens` 不会被自动转换，按不受支持的参数报错 |

选择「保留」时，任何一行里的该字段都不会被改动。首版不提供「只补缺失值」和「统一删除」。除被覆盖的字段外，其余内容按原语义保留，其中包括网关支持的供应商特定参数，例如 `top_k`、`min_p`、`thinking`、`reasoning_effort`、`response_format`、`tools`。存储时唯一的额外变化是 `body.model` 被固定为确认过的规范 ID，见 I7。

**混合情况**
- 混合任务类型：阻止提交，逐行报 `mismatched_endpoint`，建议拆成两个文件。
- 混合模型或模型不可用：未选择覆盖时阻止提交；在第 ② 步明确选择「统一改为」一个模型后即可通过（已批准：最终只用一个模型）。判断是否混合时比较的是规范模型，两个别名指向同一个模型不算混合。
- 每行参数不同：默认全部保留，并显示取值分布（最多 10 种，其余合并）；覆盖后显示「统一为 X，影响 N 行」。
- **不受支持的参数（预检直接报错）**：现状是 Chat 和 Embeddings 的请求模型及其子模型都设置了 `extra="ignore"`，执行时只转发白名单内的参数（[schemas.py:32-110](../../../apps/backend/serving/schemas.py#L32)）。所以 `max_completion_tokens`、`n`、`logprobs`、`user`、`chat_template_kwargs`、`extra_body` 等字段，用 API 提交时会被**静默丢弃**。控制台不接受这种情况：每出现一处这样的字段，都报一个 `unsupported_parameter`，给出行号和完整路径（例如 `body.messages[2].audio`），并提示怎么改（例如「`max_completion_tokens` 不会被自动转换，请在文件中改为 `max_tokens`」）。判断规则如下：
  - 合法字段的集合取自请求模型在运行时声明的字段，不在代码里另写一份清单。
  - 对**声明为子模型**的字段逐层检查，包括 `messages[i]`（`ChatMessage`）和 `response_format`（`ResponseFormat`）的外层。
  - 对**声明为任意 JSON** 的字段不展开检查，整体放行，包括 `messages[i].content`、`tool_calls`、`tools`、`tool_choice`、`thinking`、`response_format.json_schema`/`schema`。工具定义和 JSON Schema 里可以有任意键，不会被逐个拦截。
  - 现有 `/v1` API 不受影响：控制台的校验更严格，但这不算收窄 API 的能力。
- `stream:true` 和 `/v1/responses` 行：分别报 `invalid_request` 和 `unsupported_endpoint`。

**示例**：设置为「模型统一改为 model-z（当前解析到规范 ID model-z-v2），temperature 统一设为 0，max_tokens 保留」。下面是说明用的前后对照，不是一份可以直接上传的 JSONL。

```text
源文件第 2 行：
  {"custom_id":"b","body":{"model":"model-y","messages":[{"role":"user","content":"yo"}],"max_completion_tokens":64}}
预检结果：
  ✗ 行 2  b  body.max_completion_tokens  unsupported_parameter：网关不会转发此字段，不会自动转换；
          请在文件中改为 max_tokens
用户修改文件后重新上传，第 2 行变为：
  {"custom_id":"b","body":{"model":"model-y","messages":[{"role":"user","content":"yo"}],"max_tokens":64}}
存储的请求：
  {"custom_id":"b","method":"POST","url":"/v1/chat/completions","body":{"model":"model-z-v2","messages":[{"role":"user","content":"yo"}],"max_tokens":64,"temperature":0}}
```

确认页会标注三点：url 是按任务类型补上的；所选模型 `model-z` 解析为 `model-z-v2`，任务会固定使用这个 ID；配置来源里仍然保留用户选择的名称 `model-z`。

| 对象 | 是否可变 | 保留与删除 |
|---|---|---|
| 源文件 F | 不可变 | 从上传起保留 `BATCH_FILE_RETENTION_DAYS`（默认 30 天），用户可以删除；删除不影响已经提交的任务，但之后不能再用它复制 |
| 覆盖 O 与配置来源 | 随任务保存，不可修改 | 随任务删除 |
| 存储的请求文件 F′（派生文件，只在与 F 不同时生成，例如有覆盖、补全了字段、或模型被固定为规范 ID） | 不可变；只属于它所在的任务，不能再作为新任务的源 | 到期时间**不晚于 F**；删除 F 或删除任务时，F′ 一并删除（任务仍在校验时，沿用现有 GC 规则，等校验结束再回收）。崩溃时的清理见 I8 |
| 任务条目（存储的请求和结果） | 不可变 | 现有规则：任务结束后保留 7 天。只用于结果下载和「导出未成功请求」，**不能用作复制的源** |
| 复制时复用 | — | 只复用 F 和 O；新任务会生成自己的 F″，到期时间同样不晚于 F |

## 4. 代码证据：原调研基线与 #1687 目标基础

4.1–4.3 保留原基线 `5df32662` 的代码调研，4.4 是入口权限依赖，4.5 记录本次对 #1687 精确 head 的核对。涉及任务展示、状态、结果和公共路由时，以 4.5 的统一合同为准；其余新增行为仍是设计建议，不是已经实现。

### 4.1 Batch 接口与存储
- [batch_console.py](../../../apps/backend/serving/servers/routers/batch_console.py)：`/user/batches` 只支持列表（用 `after` 翻页，没有筛选和搜索）、取消、删除、下载和 webhook。模块说明写的是「Nothing here creates a batch」。没有单个任务的详情接口；`batch_view` 不返回 `paused_until`、名称或来源。
- [batches.py:393](../../../apps/backend/serving/servers/routers/batches.py#L393)：API 创建批次时，幂等检查排在角色、额度和上限检查之前。内联批次在请求内逐条校验；文件批次只检查文件的归属、用途、状态和大小，然后以 `validating` 状态入库。
- [files.py:176](../../../apps/backend/serving/servers/routers/files.py#L176)：上传以流式 multipart 分块写库，同时计算 SHA-256 和行数，并检查角色、存储上限和每小时上传次数。**只接受 API 密钥身份。**
- [validation.py](../../../apps/backend/serving/batch/validation.py)：worker 在后台按块读文件。全文件必须是同一个规范模型，`url` 必须等于 endpoint；遇到错误整批失败，且最多记录 20 条错误就停止。
- [targets.py](../../../apps/backend/serving/batch/targets.py)：Chat 和 Embeddings 按交互接口的 schema 准入，拒绝 auto 模型，别名在准入时解析为规范模型并固定下来。文件批次的准入发生在 worker 的后台校验阶段，**晚于用户看到的预览**。如果别名在这段时间里被改指到另一个规范模型，任务会照常执行，只是执行的是新指向的模型，**不会报错**。[schemas.py:74/250](../../../apps/backend/serving/schemas.py#L74) 的两个请求模型都是 `extra="ignore"`；[completions.py:707-737](../../../apps/backend/serving/servers/routers/completions.py#L707) 只转发白名单内的参数。
- [batch_store.py](../../../apps/backend/serving/storage/batch_store.py)：`batch_jobs` 有 `key_hash`、`metadata`、`paused_until`、`input_file_id`、`schema_version`、`results_expires_at` 等列；新增列通过 `_COLUMN_MIGRATIONS` 在启动时幂等添加。`batch_items.request` 保存最终请求，并带 `line_no`。`pause_user_batches` 只写 `paused_until`，暂停原因只记在日志里（[scheduler.py:985](../../../apps/backend/serving/batch/scheduler.py#L985)）。worker 会跳过 surface、target 或 schema version 比自己新的批次（见 batches.md 的 Upgrading 一节）。
- [batch_file_store.py](../../../apps/backend/serving/storage/batch_file_store.py)：上传后的文件不可变。删除只对 `status='ready'` 的行打上 `deleted_at`（一条 UPDATE，会对该行加行锁）。GC 的行为：
  - 停留在 `uploading` 超过 1 小时的行会被删除（`_STALE_UPLOAD_SECONDS`）；
  - 到期的上传文件会被标记为已删除；
  - 已删除文件的字节，要等到没有批次正在校验它、也没有读租约时才回收。

  现有代码里没有「派生文件」这个概念，旧版本的删除和 GC 逻辑也不知道派生关系。

### 4.2 身份与运行凭据
- worker 每次尝试执行前，都会调用 [`authenticate_stored_key(key_hash)`](../../../apps/backend/serving/servers/auth.py#L880)：检查密钥状态和有效期、账户状态和验证状态，以及每日额度。密钥被撤销或账户被停用时，任务以 `owner_unauthorized` 失败；额度用完或角色不足时，任务暂停。
- **`key_hash` 为 NULL 是一个特殊情况**：用户鉴权关闭时，函数直接返回匿名上下文，不管 `key_hash` 是什么；鉴权开启时，`key_hash` 为 NULL 会被拒绝。也就是说，NULL 在现有代码里只表示「创建于鉴权关闭期间」。
- 每日额度来自**密钥行**的 `quota_daily_cost_usd`（[postgres_operational.py:2259](../../../apps/backend/serving/storage/postgres_operational.py#L2259)）。每个账户最多只有一个有效密钥（唯一索引）；控制台可以创建、撤销、重新生成密钥，重新生成会先撤销旧密钥（[user_routes.py:434-681](../../../apps/backend/serving/servers/routers/user_routes.py#L434)）。
- 控制台会话通过 [`get_current_user`](../../../apps/backend/serving/servers/deps.py#L332) 鉴权，只读取 `Authorization: Bearer`，不检查额度。前端把访问令牌放在 sessionStorage，每次请求通过请求头发送（[client.ts:196](../../../apps/frontend/src/lib/api/client.ts#L196)）；刷新令牌放在 HttpOnly cookie 里，只用于刷新接口。
- Next 把 `/user/:path*` 转发到后端：`proxyTimeout` 为 1 小时，请求体上限为 100 MiB（[next.config.js:99-144](../../../apps/frontend/next.config.js#L99)），足够上传 32 MiB 的文件。

### 4.3 模型目录与前端
- `GET /user/models` 按会话可见性列出模型，不含 auto 模型；向量模型的 `supported_features` 含 `embeddings`。`ModelItem` 带有 `supported_sampling_parameters`、`max_output_length` 和 `pricing`。
- [BatchesView.tsx](../../../apps/frontend/src/components/features/batches/BatchesView.tsx) 以卡片列表展示任务，没有详情、搜索和筛选，空列表的提示只提到 API。[batches.ts:96](../../../apps/frontend/src/lib/api/batches.ts#L96) 下载结果时用 `blob()` 把整个文件读进内存。

### 4.4 前置依赖 [PR #1680](https://github.com/HarvardMadSys/hybridInference/pull/1680)（交付时为 OPEN，尚未合并；本文沿用其已批准的设计）
`GET /user/batch-capabilities` 返回 `role_may_submit`、`has_history`、`show_entry` 等字段；前端的 `useBatchCapabilities` 按账号缓存结果；统一的 Tools 区块里每个链接各自判断。当前基线**没有**这些代码（仍然是 `Internal Tools`，见 [DashboardView.tsx:156](../../../apps/frontend/src/components/features/dashboard/DashboardView.tsx#L156)）。实施前必须先合并 #1680。

### 4.5 #1687 的统一合同及设计影响（核对 head `f7e5e987`）

已阅读 [架构修订](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/docs/agents/specs/2026-10-28-unified-batch-api-amendment.zh.md) 以及相关实际 diff：
- [batch_console.py](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/apps/backend/serving/servers/routers/batch_console.py) 返回统一的 `input`、`target`、`status`、`request_counts`；`api_surface` 只作内部诊断。用户列表仍未增加创建、单个任务详情、名称搜索或状态过滤，这些才是本功能需要补充的内容。
- [renderers.py](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/apps/backend/serving/batch/renderers.py) 使用 `display_status` / `result_line`；旧 Anthropic 状态和结果渲染器已删除。新的 [前端类型](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/apps/frontend/src/lib/api/batches.ts) 定义 `BatchTarget`、`BatchInput` 和统一 `BatchStatus`；新增页面直接复用，不能另建供应商分类。
- [管理员过滤](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/apps/backend/serving/servers/routers/admin/batch.py) 已按显示状态过滤，包括 `finalizing`。用户过滤需要复用这一语义，不再将它列作要重新设计的基础能力。
- [batches.py](https://github.com/HarvardMadSys/hybridInference/blob/f7e5e9874aaa4ea3b1456fb1bbe5284689efe16b/apps/backend/serving/servers/routers/batches.py) 统一直接提交与文件输入，新增 Messages 目标，并统一读取、取消、删除和结果下载。旧 `/v1/messages/batches` 与 `/anthropic/v1/messages/batches` 仅返回 HTTP 410 迁移提示且不在 OpenAPI 中展示；历史 `msgbatch_` 任务通过统一接口保留管理能力，原有终态 webhook 仍为 `message_batch.ended`。
- Messages 目标必须通过 `anthropic-version` 请求头提供并冻结版本，版本参与其幂等摘要；JSON 中的 `anthropic_version` 字段被拒绝。因此首版 Chat/Embeddings 创建请求不携带 Messages 版本，也不从历史任务复制 `protocol_profile`。若未来开放 Messages 网页创建，版本冻结和 profile 校验必须作为新的设计内容加入，本次不顺带实现。
- #1687 没有改动 `servers/auth.py`、`servers/deps.py`、`batch/scheduler.py` 或 `batch_file_store.py`。运行仍绑定 API Key；它不解决决定 A，也不提供本设计的源文件派生、预检或复制功能。

实施以 #1687 与 #1680 整合后的 `dev` 为起点。两者都会影响 Batch 控制台代码，整合时保留 #1680 的 capability/权限判断和 #1687 的统一 DTO/渲染语义，再添加创建功能。本轮仅审查设计影响，没有重新执行 #1687 的测试，也不把 PR 描述中的验证结果当作本轮独立验收。

## 5. 关键不变量（以下均为设计要求）

- **I1 归属**：所有新的 `/user/*` 路由都只读写会话用户自己的数据。别人的批次、文件和 `copied_from` 一律返回 404，管理员通过这些路由也一样（不沿用公共 `/v1/files` 中管理员可跨用户访问的 `_lookup_scope`）。
- **I2 登录身份与运行凭据分开**：会话决定任务归谁所有，运行凭据决定 worker 以谁的身份执行，两者必须属于同一个账户。
  - 推荐方案（决定 A）：确认页显示的是该账户当前有效密钥的**标识**：密钥 id、脱敏前缀，以及一个不透明的**凭据修订号**（服务端由密钥行 id 与其存储哈希做带域分隔的单向摘要后截断得到），从不出现原始密钥或哈希。修订号必不可少：现有「重新生成」会原地改写同一行的哈希与前缀（[postgres_operational.py:2548](../../../apps/backend/serving/storage/postgres_operational.py#L2548)），只比对 id 不能发现轮换。id 与修订号会写进提交请求，并纳入摘要。
  - 服务端提交时再查一次当前有效密钥。如果 id 或修订号与确认时不一致（这期间密钥被轮换、撤销、重新生成或新建过），返回 409 `credential_changed`，要求用户重新确认；**不会**悄悄改用另一把密钥。
  - 标识一致时，把该密钥的 `key_hash` 写入任务，再用 `authenticate_stored_key` 生成和 API 路径相同的 `user_ctx`，交给准入使用。
  - 不让用户粘贴密钥，不自动创建密钥，不把会话令牌交给 worker。
  - 对用户的含义：会话过期、退出登录、关闭浏览器，都**不会**停止已经提交的任务；撤销或重新生成这把密钥则会让任务失败。
  - 控制台创建的任务**永远不写入 NULL `key_hash`**：没有密钥时拒绝提交（`no_worker_credential`）；关闭了用户鉴权的部署不开放控制台提交，以免与「匿名」语义混淆。
- **I3 账户型任务要与旧 worker 隔离**（只在决定 A 选择账户原生执行时适用）：任务带上显式的 `credential_kind='account'` 和新的 `schema_version`，旧 worker 按现有规则会跳过它，绝不会把 NULL `key_hash` 当作匿名、或当作拒绝来处理。新 worker 对这类任务改用账户校验。推荐方案不需要这一条。
- **I4 权限只在后端判定**：上传、预检、提交都检查 `may_use_batch(role)`；提交时还要检查额度、未结束任务上限和运行凭据。上传和预检不收费，所以每日额度为 0 时仍然可以做；新提交会被拒绝。
- **I5 CSRF**：新增的写接口只接受请求头里的 Bearer 令牌（`get_current_user`），不接受 cookie 鉴权；CORS 配置不变。如果以后改成 cookie 会话，这些接口必须同时加上 CSRF 令牌。
- **I6 幂等的对象是「提交意图 + 键」，不是内容去重**：
  - **一个意图对应一个键**。用户点「确认提交」时，客户端生成 `console-<uuid>`，再把键、请求体和创建时间作为**一条待确认记录**，写入 `localStorage` 的 `batch-submit:<user_id>`。同一个账户同一时间只保留一条。之后的超时重试、重开标签页、页面恢复，用的都是这个键和这份不变的请求体。
  - **跨标签页协调**。读写这条记录、发出提交请求时，都要先拿到 Web Locks（`navigator.locks`，锁名同上）。不支持 Web Locks 的浏览器改用 IndexedDB 的读写事务原子创建、读取和更新同一账户的待确认记录，不能用非原子的 localStorage 心跳模拟互斥；该模式下 IndexedDB 是记录的唯一来源。两种能力都不可用时明确提示无法安全创建新意图，不悄悄降级。别的标签页看到待确认记录后，先按这条记录恢复；在结果确定之前，不能新建意图。
  - **结果确定后清除记录**：200 → 跳转到任务；409 内容冲突或 422 等确定性错误 → 显示原因；410 → 提示「这次提交建出的任务已被删除」。410 **不会**自动生成新键，用户必须再次明确点提交，才会产生一个新意图。
  - **两次独立的提交可以建出两个任务**。用户用不同的键先后提交两次，即使输入完全相同，也会得到两个任务，这是预期行为。
  - **账号隔离**。记录按 `user_id` 分区，服务端响应里的 `user_id` 必须与当前账号一致。切换账号后，不显示、也不重放其他账号的记录；回到原账号时再恢复。
  - **保留的键不能永远代替结果（实现修订）**。服务端幂等记录只保证保留到任务结束后 24 小时（`_IDEMPOTENCY_LIVE`），过期记录在锁内会被当作未使用并删除；因此「一直用原键重试」本身不能永久防止重复建任务。恢复顺序如下：
    1. 先做**只读查询** `GET /user/batch-submissions/{key}`：控制台任务在行上保存了键和请求摘要（`batch_jobs.idempotency_key`、`console_spec.request_digest`），所以记录过期后、任务行被保留期清除前，仍能查到 `created`；记录为墓碑时为 `deleted`；两者都没有时为 `unknown`（无法区分「从未创建」与「已创建并已清除」，因此永不回答「未创建」）。
    2. 查询结果为 `unknown`，且意图创建时间距今仍在保证窗口内（上下文返回的 `submission_replay_seconds`，即 24 小时，前端再减 1 小时余量）时，才用**原键原请求体**自动重发；此时若曾创建过任务，其记录必然仍在，服务端会重放。
    3. 超出窗口仍为 `unknown` 时，记录变为「过期未确认」：**停止自动 POST**，保留记录，展示说明，只提供「打开任务列表」「作为新任务再次提交」（明确确认，提示可能再建并计费一个任务，生成**新键**）和「忽略」。不会悄悄生成新键。
    4. 服务端兜底：控制台提交在事务内找不到有效记录时，还会按 `(owner, created_via='console', idempotency_key)` 查找仍保存的任务行；摘要相同则返回原任务，不同返回 409。公共 `/v1/batches` 的过期语义不变。
  - **短暂拒绝不算结果**。`429 scan_busy`（同一用户已有扫描在进行，例如超时后的重试赶上了第一次请求）、5xx、网络错误都保留意图并用同一键稍后重试；只有确定性答复（200、409、410、413、403、422、额度或上限 429）才清除意图。
  - **服务端**复用 `batch_idempotency`（沿用内部所有者和 `api_surface` 命名空间，不将其变成产品分类；同键不同内容返回 409；任务被删除后返回 410；记录一直保留到任务结束后一天）。请求摘要包括：`source_file_id` 和对应的不可变 SHA-256、endpoint、O、确认过的规范模型、名称、描述、`copied_from`、密钥标识。
  - **重放先于其他检查**。只要会话属于同一账户，重放在角色、额度、凭据检查之前就返回已经建好的任务。即使这期间额度用完或密钥轮换，也照样返回，与现有 API 的行为一致。控制台提交总带键，因此**不做事务外的未结束任务上限预检**：上限只在持有用户准入锁的事务内、在再次检查重放之后判断，避免同键的并发胜者先占满上限、使重试被误判为 429。
  - **这个机制只防止「意外多建一个任务」**。单条请求仍可能因为 worker 重试，或因为费用未知的尝试而被计费不止一次，这是现有的行为（见 batches.md 的 Costs 一节）。
- **I7 检查时机与模型绑定**：第 ① 步只识别文件，不做模型准入，所以文件里写的模型即使不可用，也可以在第 ② 步统一替换。预检只读，结果只用于提示。
  - **预检的模型访问上下文**：会话上下文本身不含 `disabled_models`；预检和模型目录使用「会话用户 + 账户偏好中的 `disabled_models`」，与 `/user/models` 和 API 密钥路径一致，且不需要密钥或额度。被禁用或不可见的模型与不存在的模型返回相同的 `model_not_found` 文案。提交时改用 `authenticate_stored_key` 返回的密钥上下文。
  - **提交时绑定模型**。请求体带上 `confirmed_model = {requested, canonical}`，也就是第 ③ 步展示的解析结果。服务端在准入前重新解析一次；如果结果和 `canonical` 不同（别名在这期间被改指），返回 409 `model_resolution_changed`，要求用户重新预检、重新确认，**不会**改用新的目标。未统一覆盖模型时，必须校验每一行原模型名都解析到这个规范 ID；不能只检查文件首行，允许同指一个规范 ID 的多个别名。
  - **存储时写死规范 ID**。存储的请求里，`body.model` 直接写成确认过的规范 ID（必要时为此生成派生文件 F′）。用户原来选的别名保留在配置来源里，预览中也会显示「别名 → 规范 ID」。这样 worker 后台校验时解析的就是规范 ID 本身，之后再改别名，不会把这个任务改到别的模型上。
  - **worker 校验仍是最终判定**。如果在提交之后、worker 校验之前，用户失去了这个模型的权限，或模型被下线，整批以 `failed` 结束，一条请求也不执行（现有行为）。
  - **不承诺模型权重版本**。规范 ID 固定的是路由层面的模型标识，不是模型权重的版本。管理员正常调整供应商或路由配置，可能改变同一规范 ID 背后实际提供服务的上游，这不在本设计的保证范围内。
- **I8 源文件生命周期与提交的原子性**：
  - **扫描期间持有读租约**。预检和提交扫描 F 时都持有读租约（沿用现有的 `read_lease_until` / `batch_read_leases`），保证字节在扫描过程中不被 GC 回收。用户在这期间删除 F 的操作照样生效。
  - **F′ 先作为临时文件写入**。提交时，F′ 以 `status='uploading'` 写入，并带上 `derived_from_file_id=F`，这时它不可见、也不可用。
  - **发布任务时原子收养**。在**同一个事务**里依次完成以下几步：
    1. 按现有准入锁顺序（账户行 `FOR SHARE` → 用户准入锁）之后，再 `SELECT … FOR UPDATE` 锁住 F，随后锁派生文件行；删除和 GC 使用兼容的锁顺序（删除：先 F 再其派生文件）。注意不能沿用公共创建的 `_pin_input`：它取 `FOR KEY SHARE`，与删除语句 `UPDATE … SET deleted_at` 所取的 `FOR NO KEY UPDATE` 相容，无法使两者串行；`FOR UPDATE` 与之冲突，两者因此串行执行。
    2. 重新确认 F 属于当前用户、`deleted_at IS NULL`、尚未到期。
    3. 预分配新任务 ID，将 F′ 改为 `ready` 并绑定这个 ID，设置 `expires_at = min(F.expires_at, 正常保留期)`。这些变化尚未提交，对事务外不可见。
    4. 使用同一数据库连接写入任务、输入文件引用和幂等记录，再一次性提交；若幂等记录已存在，整次派生文件收养操作回滚，返回原任务。需要抽取接受现有连接的事务内插入函数，不能在外层事务里调用另开连接、自己提交的现有 `create_batch`，也不能在 F′ 仍为 `uploading` 时调用要求文件已就绪的输入引用逻辑。

    只要 F 已经被删除或到期，整个事务回滚，返回 `source_unavailable`。因此，在扫描或提交过程中被删除的源文件，不会变成一个可以再次使用的 F′。
  - **崩溃也能清理**。进程崩溃后留下的 F′ 一直处于 `uploading` 状态，现有 GC（包括旧版本）一小时后会把它删掉。新版 GC 也检查「有 `derived_from_file_id`、没有 `adopted_by_batch_id`、超过一小时且无有效使用租约」的文件，锁行后再次确认再回收，覆盖异常的 ready 孤儿文件；不能回收尚在正常提交中的临时文件。请求中途出错时的 catch 清理只是尽力而为的补充，不能依赖它。
  - **F′ 的归属与容量**。F′ 只属于收养它的任务：不会出现在复制入口里；新版本也不接受它作为任何新批次的 `input_file_id`。它计入存储容量，但不计入每小时上传次数。
  - **不能借任务条目绕开源文件的删除或过期**。复制只能从 F 开始；任务条目只在任务的保留期内，用于结果下载和「导出未成功请求」。复制不会修改 F 的 `expires_at`。
- **I9 解析有上限，不阻塞**：服务端每次读 4 个块，从不把整份文件读进内存；每处理 500 行让出一次事件循环；沿用现有的单行、总大小和行数上限，并且对覆盖后的结果也执行这些上限。每个进程最多同时扫描 2 个文件，同一进程内每个用户最多 1 个，超出返回 429 `scan_busy`；这是进程级保护，不宣称是跨实例全局限流。单次扫描最长 120 秒。返回内容也有上限：错误最多 1000 条，附带总数；预览 3 行，每行最多 16 KiB。日志不记录行内容。浏览器不解析文件。
- **I10 清理**：上传中途中断时调用 `abort_upload`。提交失败或命中幂等记录时，在同一个请求里删除这次写出的 F′；即使这一步没执行，I8 的 GC 也会把它回收。预检不创建任务或派生文件、不保存校验报告，只维护扫描所需的短期读租约。
- **I11 取消与费用**：沿用现有语义：未开始的请求取消，已在执行的请求完成并计费，封存后可以下载部分结果。只显示已知费用，未知次数单独列出，不按 0 计算，也不做预估。
- **I12 继承 #1687 的 API 合同**：本功能不额外改变 #1687 合并后的 `/v1/*` 行为、错误和限额，也不恢复已停用的 Message Batches 功能（保留 #1687 的 410 迁移提示）。控制台独有的规则（BOM、重复键、补全 url、拒绝 `unsupported_parameter`、绑定规范模型）只在 `/user/*` 创建时生效；创建类型明确限定为 chat/embeddings，不能因为后端 `TARGET_URLS` 已包含 messages/responses 就自动开放首版 UI。
- **I13 预览与认证的边界**：预览展示的是存入任务的请求，不包括适配器在执行时做的协议转换；截断过的内容会明确标出「已截断」，不宣称预览是完整的。前端上传要复用现有的认证客户端：用 XHR 包装出上传进度，但令牌刷新、401 重试、账号一致性检查都走 `client.ts` 现有逻辑，不另写一套认证。

## 6. 技术附录：API、数据与代码触点（建议，未实现）

所有路由都用会话鉴权，并返回 `Cache-Control: no-store`。`BATCH_ENABLED` 关闭或没有存储时返回 404。

`/user/batches` 是统一 Batch 服务的会话适配层：复用公共的准入、存储、调度和结果逻辑，不通过浏览器持有 API Key 去调用 `/v1/batches`，也不另建一套执行管线。`console_prepare` 仅增加源文件预检、显式覆盖和配置来源记录。前端复用 #1687 的 `ConsoleBatch` / `BatchTarget` / `BatchInput` / `BatchStatus`；复制资格为 `target ∈ {chat, embeddings}` 且 `input=file` 且源文件可用，后端再次验证。列表和标准结果管理仍覆盖所有目标。

| 路由 | 用途 | 门槛 |
|---|---|---|
| `GET /user/batch-create/context` | 返回限额、用量、额度，以及运行凭据的标识（`key_id` 和脱敏前缀，从不返回原始密钥或哈希） | 本人 |
| `POST /user/batch-files` | 上传源文件；复用从 `files.py` 抽出的上传核心，`/v1/files` 不变 | 角色、存储空间、上传频率 |
| `GET` / `DELETE /user/batch-files[/{id}]`，`GET …/{id}/content` | 最小清理入口；下载源文件或 F′ | 本人 |
| `POST /user/batch-preflight` | 只读的识别和预检；请求显式区分 identify / validate 阶段，endpoint 可由服务端识别，不能再用是否传 endpoint 区分阶段 | 角色 |
| `POST /user/batches` + `Idempotency-Key`（必需） | 创建 | 角色、额度、上限、运行凭据 |
| `GET /user/batches/{id}` | 详情，含 `creation` 和源文件状态 | 本人 |
| `GET /user/batches?status=&q=` | 现有列表增加名称/ID 搜索及统一显示状态过滤，复用 `display_status_sql`，包含 `finalizing` | 本人 |
| `GET /user/batches/{id}/unfinished-requests` | 结果可用后，导出 Chat/Embeddings 的 failed/expired/cancelled 条目为 `{custom_id,method,url,body}`；Messages/Responses 暂不提供此输入重建入口 | 本人 |

```jsonc
// POST /user/batch-preflight
{"source_file_id":"file-…","endpoint":"/v1/chat/completions","overrides":{"model":"model-z","temperature":0}}
// → 200
{"source":{"id":"file-…","sha256":"3f9a…","lines":2000,"expires_at":1795521600},
 "detected":{"endpoints":{"/v1/chat/completions":2000},
             "models":{"old-model":{"rows":1200,"available":false},"model-y":{"rows":800,"available":true}}},
 "params":{"temperature":{"0":800,"0.7":1100,"(unset)":100}},
 "model_resolution":{"requested":"model-z","canonical":"model-z-v2"},
 "valid":false,"error_count":3,"errors_truncated":false,
 "errors":[{"line":17,"custom_id":"q-17","field":"body.messages","code":"invalid_request","message":"body.messages: Field required"},
           {"line":40,"custom_id":"q-40","field":"body.max_completion_tokens","code":"unsupported_parameter",
            "message":"Not forwarded by the gateway and not translated; replace it with max_tokens in the file"}],
 "preview":[{"line":1,"custom_id":"q-1","filled":["method"],"truncated":true,
             "request":{"custom_id":"q-1","method":"POST","url":"/v1/chat/completions","body":{"model":"model-z-v2","messages":["…(已截断)"],"temperature":0}}}]}

// POST /user/batches   Idempotency-Key: console-7c1e…
{"source_file_id":"file-…","source_sha256":"3f9a…","endpoint":"/v1/chat/completions",
 "overrides":{"model":"model-z","temperature":0},
 "confirmed_model":{"requested":"model-z","canonical":"model-z-v2"},
 "credential":{"key_id":"k_123"},
 "name":"eval-0420","description":"","copied_from":null}
// → 200 详情对象；409 no_worker_credential / credential_changed / model_resolution_changed /
//   source_unavailable / 幂等冲突；410；422 含错误列表；429；413
```

提交的处理顺序：
1. 幂等查重。同一账户的会话命中记录时，直接返回已有任务。
2. 检查角色。
3. 核对 `credential.key_id` 与凭据修订号是否就是当前有效密钥，然后调用 `authenticate_stored_key`。
4. （不在此处检查未结束任务数上限：见 I6，上限在第 8 步的事务内、重放复查之后判断。）
5. 校验源文件的归属、状态和 SHA，并为它取得读租约。
6. 流式扫描：应用覆盖，按 `confirmed_model` 核对模型解析结果，逐行准入，任何 `unsupported_parameter` 都直接拒绝。
7. 只有内容与 F 不同，或者需要把模型固定为规范 ID 时，才以临时状态写出 F′。
8. 在 I8 所述的同一个事务里锁住 F 并再次检查，通过抽取出的事务内插入函数完成派生文件收养、任务与幂等记录写入；保留现有准入语义，另写入新列。写入的字段分别是：
   - 输入方式：`api_surface` 取 `openai_file`；
   - 输入文件：`input_file_id` 取 F′（有派生文件时）或 F；
   - 运行凭据：确认过的有效密钥（其存储哈希只在服务端使用）；
   - 阶段：`phase` 取 `validating`。
9. 唤醒 worker。

`metadata` 字段留给用户自己使用，控制台不占用。

**错误清单下载**：前端根据 `errors` 生成 CSV 或 JSONL；错误超过 1000 条时，文件中注明总数。服务端不保存错误清单。

**数据变更**（沿用 `_COLUMN_MIGRATIONS` 的加列方式，所有列都可为空）

| 表.列 | 说明 |
|---|---|
| `batch_jobs.display_name` / `description` | 名称 1–128 字，不含控制字符，不要求唯一；描述 ≤1000 字 |
| `batch_jobs.created_via` | `console`；已有行为 NULL，表示 API 创建 |
| `batch_jobs.console_spec` (JSONB) | `{source_file_id, source_sha256, source_filename, endpoint, overrides, confirmed_model:{requested, canonical}, credential_key_id, copied_from}` |
| `batch_jobs.paused_reason` | `quota` / `role`。旧 worker 不写这一列，界面统一显示为「已暂停」 |
| `batch_files.derived_from_file_id`、`adopted_by_batch_id` | 只用于 F′（I8）。删除 F、删除任务或清除任务时，在同一个事务里把 F′ 标为已删除，再由 GC 回收；不用外键级联，以免绕过读租约。新版的 `_check_file` 拒绝把派生文件作为 `input_file_id` |

**兼容性与混合版本的边界**（这里列的是设计要求，不涉及部署项目本身）
- **worker 能否执行**：采用推荐的密钥绑定方案时，首版控制台任务是存储上沿用名称 `openai_file` 的 Chat/Embeddings 文件任务，`schema_version` 不变。#1660 及以后的 worker 支持这两个目标；这个结论不适用于 #1687 新增的 Messages 输入组合。部署、排空和回滚还必须遵循 #1687 的架构修订，不能把执行兼容推导成全部生命周期兼容。
- **生命周期保证需要新版网关**：派生关系、F 删除时联动删除 F′、回收「未被收养」的文件，这三项保证只有新版网关和新版 GC 能提供。在新旧网关混跑期间，或者回滚以后：
  - 旧网关删除 F 时，不会联动删除 F′，F′ 会一直保留到它自己的到期时间（不晚于 F 原本的到期时间）；
  - 旧网关不认识派生标记，所以 API 可以拿 F′ 作为新批次的输入；
  - 旧 GC 只能回收仍处于 `uploading` 状态的临时 F′（超过一小时）。
- **安全边界**：控制台的创建入口与新版后端同一个版本发布；只有在全部网关进程都升级到新版之后，「删除源文件会同时删除派生文件」这一条才对外成立。如果需要回滚，残留的 F′ 是有界的：它们带有 `derived_from_file_id`，可以查询出来；数量和容量都受用户的存储上限约束，最晚在 F 的原到期时间之前过期。
- **账户原生执行**（I3）：如果决定 A 选择了这一方案，回滚之前必须先排空这类任务。
- `/v1/*` 返回的对象不包含新增的列。

**限额**：全部沿用现有设置，不新增设置项。以下是代码常量：错误上限 1000、预览 3 行、单行预览 16 KiB、扫描并发 2 / 每用户 1、扫描超时 120 秒。文件批次的执行窗口固定为 24 小时。

**代码触点**：`servers/routers/batch_console.py`（新路由、`batch_view` 新字段）；`servers/routers/files.py`（只抽取上传核心）；新增 `batch/console_prepare.py`（扫描、覆盖、准入、统计，写 F′，复用 `validation.iter_lines` 和 `targets.admit_item`）；`batch/admission.py`（透传新列）；`storage/batch_store.py`（新列、筛选、`pause_user_batches(reason)`、联动删除 F′）；`storage/batch_file_store.py`（派生文件的写入、收养、联动删除和 GC）；`batch/scheduler.py`（传入暂停原因）。`servers/auth.py` 在推荐方案下不改动。前端：`lib/api/client.ts` 和 `lib/api/batches.ts`（基于现有认证客户端封装 XHR 上传，支持进度显示和取消；跨标签页的提交意图记录）、`components/features/batches/`（向导、详情、筛选）、`app/dashboard/batches/new`、`[id]`。文档：`docs/developer/batches.md`。

## 7. 验收场景与实施阶段

| # | 场景 | 期望结果 |
|---|---|---|
| A1 | 单一模型的 Chat 文件，模型名本身就是规范 ID，不做覆盖，直接提交 | 正常执行到完成；`input_file_id` 就是 F，不生成 F′；详情页显示配置来源 |
| A2 | 复制 A1，把模型改为 B、temperature 统一设为 0 | 进入第 ② 步时已带好原设置；预览中每一行都是 B 和 0；提交前没有任何执行；新任务的 `copied_from` 正确；F 的到期时间不变 |
| A3 | 复制 API 创建、`input=file`、target 为 Chat/Embeddings 且输入仍保留的任务；复制 `input=inline` 任务 | 前者可以复制（O 为空）；后者按钮置灰并说明原因；不按厂商或 api_surface 字符串决定资格 |
| A4 | 每行 temperature 取值不同：先保留，再统一设为 0.2 | 保留时语义不变，也不生成 F′；统一设置后所有行（包括原本未设置的）都是 0.2，并显示受影响的行数 |
| A5 | Embeddings 文件，部分行含 `dimensions` | 不显示文本参数；`dimensions` 原样保留；执行成功 |
| A6 | 文件含空行、非法 JSON、重复 custom_id、缺少 messages、`stream:true`、BOM、重复键 | 每个问题都有行号、custom_id 和原因；不能提交；可以下载错误清单；不创建任务 |
| A7 | 同一文件混有 chat 和 embeddings 行 | `mismatched_endpoint`，整批阻止 |
| A8 | 文件中的模型已不可用，或混有多个模型 | 第 ① 步只标出 ⚠，不阻止；不覆盖时第 ③ 步阻止；在第 ② 步统一改为可用模型后通过 |
| A9 | 文件中有 `body.max_completion_tokens`、`body.user`、`body.messages[0].audio`，以及 `tools`/`response_format.json_schema` 中的任意键 | 前三处各报一条 `unsupported_parameter`，带行号、路径和修改建议，整批阻止；工具定义和 JSON Schema 里的键原样放行；把文件中的字段改为 `max_tokens` 后可以通过 |
| A10 | 预检后、提交前收回模型权限；或在提交后、worker 校验前收回 | 前者返回 422，不创建任务；后者整批 `failed`，一条请求也不执行 |
| A11 | 别名 `model-z` 在预览时指向 `model-z-v2`，确认前被改指到 `model-q` | 提交返回 409 `model_resolution_changed`，不创建任务；重新预检并确认后，存储的请求使用新的规范 ID。如果改指发生在任务创建之后，任务仍按 `model-z-v2` 执行 |
| A12 | 执行中撤销或重新生成密钥；确认后、提交前轮换密钥；没有密钥的用户提交；退出登录或关闭浏览器 | 依次为：`owner_unauthorized`；409 `credential_changed`，要求重新确认；409 `no_worker_credential`，不会自动建密钥，`key_hash` 永远不是 NULL；任务照常执行 |
| A13 | 执行中额度用尽；执行中被降级；每日额度为 0 时上传和预检 | 显示「已暂停：额度」或「已暂停：角色」，用户仍可取消、下载、删除；额度为 0 时可以上传和预检，提交返回 429 |
| A14 | 源文件已删除或过期后复制；预检扫描或提交扫描过程中删除 F；在 F′ 写出后、收养前让进程崩溃 | 复制回到第 ① 步并保留设置；扫描中删除时提交返回 `source_unavailable`，不留下可用的 F′；崩溃后残留的 F′ 由 GC 回收；不会从任务条目恢复出源文件 |
| A15 | 同一个提交意图：响应丢失后重试、重开标签页后恢复、两个标签页同时恢复；同一个键配上不同内容；任务删除后再重放 | 只建出一个任务，不留多余的 F′；同键不同内容返回 409；任务删除后返回 410，且不会自动生成新键。（单条请求仍可能因 worker 重试或费用未知的尝试被多次计费，这是现有行为） |
| A16 | 用户两次独立提交相同输入（两个不同的键） | 建出两个任务，符合预期 |
| A17 | 执行中取消 | 状态从 `cancelling` 变为 `cancelled`；部分结果可以下载；「未成功请求」导出中包含被取消的条目；已知费用和未知费用分开显示 |
| A18 | Chat/Embeddings/Messages/Responses 的文件或直接提交任务，以及历史 `msgbatch_` 任务；覆盖进行中、过期、取消、finalizing | 全部使用统一状态与计数，过期数不重复累加；旧任务也显示实际进度和统一终态；显示 finalizing 时用户过滤同样能命中 |
| A19 | 用户 B 访问 A 的批次、文件、`copied_from`、导出接口；同一浏览器先后登录 A 和 B | 全部返回 404，搜索结果中看不到 A 的任务；B 登录后看不到、也不会重放 A 待确认的提交 |
| A20 | 预检 32 MiB / 50,000 行的文件，同时有交互请求；上传过程中访问令牌过期 | 交互请求延迟没有明显上升；预检在超时前完成；浏览器不卡顿；上传走现有的刷新流程，并且始终以同一账号完成 |
| A21 | 只带 cookie、不带 Authorization 的跨站 POST；`BATCH_ENABLED` 关闭 | 分别返回 401 和 404，都不写入任何数据 |
| A22 | 回归 #1687 的 `/v1/batches`、`/v1/files`、在线 Messages 及已停用路由 | 统一 API 的 Chat/Embeddings/Responses/Messages 行为保持，Messages 版本冻结与幂等保持；旧两条 Message Batches 路由保持410且不在 OpenAPI；含 `max_completion_tokens` 的文件经 API 提交仍保持 #1687 后的行为 |
| A23 | 同为 `input=file`，分别选择 chat 和 messages 任务复制；下载新的 Messages 文件任务和旧 msgbatch_ 结果 | 仅 chat 在首版可复制；Messages 提示网页暂不支持该类型，仍能查看/管理/下载。结果均使用统一外层，原生 Messages 响应留在 response.body，不恢复旧 result 格式；非文件任务只显示全部结果下载，不显示实际返回相同内容的成功/错误按钮 |

| A24 | 带显式 url 的 Chat/Embeddings 文件；省略 url 但结构唯一的文件 | 自动识别整份文件，正常流程没有必选类型控件；模型与参数自动匹配；确认页显示识别结果 |
| A25 | 缺少信息产生歧义；显式 url 与结构冲突；文件混合类型；未支持 endpoint | 只有真正的歧义展示补充选择；其余逐行报错，不能通过手选类型绕过；手选也必须通过完整 schema 校验 |

**实施阶段**
0. 以 #1687 和 #1680 整合后的 dev 为实现基线，核对权限入口、统一 DTO、结果格式、状态过滤的组合。前端与后端按统一合同发布，不为旧供应商展示格式再加一套 UI 兼容分支。
1. 复用 #1687 的标签、状态、计数和结果渲染；补充新增列、`paused_reason`、详情接口与页面、用户名称搜索及状态过滤、Chat/Embeddings 的「导出未成功请求」和空列表文案。这一步不依赖创建功能，API 用户也能受益。
2. 上传、只读预检、最小清理入口、`console_prepare` 模块，以及向导第 ①–③ 步的界面。与阶段 3 放在同一个 PR 栈里上线，**不加功能开关**。
3. 提交、提交意图幂等、绑定运行凭据和规范模型、F′ 的临时写入与原子收养、复制（采用决定 B 的文件范围）。
4. 更新文档；运行 `make test`、`make test-db-local` 和前端测试，并对照上表逐项验收。

**验证环境约束（仅适用于实施阶段）**：Claude 在当前的协作服务器上运行测试和预览。Codex 在用户指定的项目开发环境中独立验证：先核对仓库、分支和目标部署，再执行，并在验证记录里写明实际执行位置。两方的结论分别记录，不互相替代。本设计任务本身不运行任何测试或预览。

以下常规技术选择直接采用默认方案，不再单独确认：预检同步执行，不建后台任务或草稿表；草稿只保存在浏览器；错误清单由前端生成；默认名称为 `<文件名>-<模型>-<YYYYMMDD-HHmm>`；不新增索引和设置项。

## 8. 首版实施选择（沿用已向产品说明的推荐方案）

以下事项已经批准或属于校验规则，这里不再重复询问：
- 多模型或模型不可用的文件，只能通过明确的统一覆盖归一为一个最终模型；
- 不受支持的参数在预检时直接报错（这是「预览即存储的请求」这一约定的一部分）。

**决定 A：控制台任务用什么身份执行。**
- **推荐：绑定账户当前有效的 API 密钥**。由服务端查找，用户不需要粘贴密钥，系统也不会新建密钥。worker、鉴权、额度和撤销的逻辑都不用改，行为与 API 创建任务完全一致。对用户来说，这意味着：
  - 登录过期、退出登录、关闭浏览器，都**不会**停止已经提交的任务；
  - 撤销或重新生成这把密钥，**会**让未结束的任务失败；
  - 没有密钥就不能提交，页面会引导用户去密钥页自己创建；
  - 确认之后如果密钥被轮换，需要重新确认。
- **备选：账户原生执行**。不依赖密钥，换密钥也不影响任务。代价是：
  - 需要改造鉴权和 scheduler；
  - 要重新定义额度的来源（现在额度存在密钥那一行上）；
  - 要按 I3 隔离旧 worker；
  - 需要单独做一次安全评审。
- **不采用：自动替用户创建密钥**。这等于悄悄生成长期密钥，还会与「每个账户只能有一把有效密钥」的规则冲突。
- **本次采用推荐方案**。密钥页提醒「撤销会影响 N 个任务」属于可选增强，不在首版必做范围。

**决定 B：首版的复制范围。**
- **推荐**：首版只支持复制两类任务：控制台创建的任务，以及输入文件仍然保留的 API 文件批次（Chat/Embeddings，包括本功能上线前创建的）。
- **暂不支持的类型**：`input=inline` 的直接提交任务没有可复用输入文件；`target=messages/responses` 不在首版创建类型内，哪怕有文件也不能网页复制。按钮说明具体原因，不按供应商分类；标准管理和结果下载不受影响。
- **为什么首版暂缓内联批次**：它没有现成输入文件，需要另行设计从未过期的任务记录生成输入的路径，以及对应的来源和保留期规则。技术上可以支持；本次建议暂缓，是为了把首版限制在已确定的文件流程内，而不是因为内联复制必然违反保留期原则。
- **本次采用上述文件任务范围**，不扩大到内联输入重建或其他请求类型。

## 9. 来源与证据边界

- **外部来源**：初版引自 2026-10-27 的调研报告；2026-10-28 再次核对 Fireworks、百炼与 Bedrock 官方操作文档以确认类型选择交互，未登录控制台实测。
  - Fireworks（<https://docs.fireworks.ai/guides/batch-inference>）：官方文档说明可以在控制台的 Batch Inference 页面「Create Batch Inference Job」，选择模型和数据集；「可选设置」具体包含哪些字段未知。
  - 阿里云百炼（<https://help.aliyun.com/zh/model-studio/batch-inference>、<https://help.aliyun.com/zh/model-studio/openai-compatible-batch-file-input>）：「创建批量推理任务」需要填写名称、描述、最长等待时间，上传 JSONL，并提供示例文件下载；任务列表支持搜索和取消，可以下载错误文件；同一文件内的模型必须一致。
- **借鉴与不借鉴**：借鉴了任务名称和描述、示例文件、列表搜索与状态、取消和错误文件下载、单文件单模型。没有借鉴价格、折扣、限额、保留期、执行窗口（例如百炼的 1–14 天）和任何承诺；本网关的计费规则见 [batches.md](../../developer/batches.md)。
- **证据不足的部分**：调研中没有找到原生 Batch 产品在 UI 上提供列映射、试跑或费用预估的证据，但这不等于这些平台没有。本文也没有因为某个平台的 API 或 CLI 支持某项能力，就推断它的 UI 原生支持。
- **事实与建议的区分**：第 4.1–4.3 节来自原基线；第 4.5 节已按 #1687 head `f7e5e987` 的实际代码和架构修订更新。[PR #1680](https://github.com/HarvardMadSys/hybridInference/pull/1680) 的内容依据其已批准的设计。Codex 本次核实 #1680 与 #1687 均为 OPEN、尚未合并；设计初稿未复跑这些测试；实现阶段的测试与独立验收另记于 [验收记录](../../reviews/2026-10-28-batch-ui-create.md)，未进行生产部署。第 1–3 节和第 5–8 节中的新增行为都是设计建议，其中首版范围和自动识别流程已获准实施；其他扩展仍不在本次范围。本文不包含任何部署环境或私有基础设施的信息。

## 10. 实现说明（2026-10-28，实现层面的选择，不改变已确认的产品行为）

- **整合基线**：本地依次合并 #1687（`477f171c`）与 #1680（`aba29b25`）到 `dev` `df40a9a8` 之上，得到 `319544fc`；功能改动在其后；交付前再合入 `dev` `453884a3` 与 #1687 `f7e5e987`。冲突按「保留 #1687 的统一 DTO/文案 + #1680 的能力判断/不可用状态」解决。
- **代码位置**：扫描与预检 `apps/backend/serving/batch/console_prepare.py`；控制台创建路由 `servers/routers/batch_console_create.py`（上下文、源文件、预检、提交查询、创建）；详情、筛选、导出在 `servers/routers/batch_console.py`；`/v1/files` 与控制台共用抽出的 `files.store_upload`；模型准入共用 `targets.resolve_model`（从 `admit_item` 抽出，不另写一套）。
- **两遍扫描**：第一遍识别（严格 JSON、信封、类型、统计），第二遍在类型确定后逐行严格校验（`model_validate(strict=True)` + 按请求模型运行时字段逐层查找未声明字段）、应用覆盖、准入并写出 F′。两遍都在同一个读租约下流式读取。
- **类型判定细则**：显式 `url` 优先；无 `url` 时只有 `messages` 无 `input` 判为 Chat，只有 `input` 无 `messages` 判为 Embeddings；文件中确定类型的行只有一种时，无法判定的行按该类型完整校验；没有任何行能判定时才 `needs_type_choice`。选择与文件已确定的类型不同会报错。
- **派生文件**：`batch_files.derived_from_file_id`/`adopted_by_batch_id`；F′ 以 `uploading` 写出，只由 `create_console_batch` 事务置为 `ready` 并收养；删除 F、删除任务、保留期清除任务时同事务标记 F′ 删除；GC 另外回收「超过一小时、ready 但未被收养或收养它的任务已不存在」的 F′；校验中的任务仍按现有规则固定其输入字节。公共 `/v1/batches` 拒绝以 F′ 作为输入；F′ 不计每小时上传次数，但计入存储容量。
- **新列**：`batch_jobs.display_name / description / created_via / console_spec / paused_reason`，`batch_files.derived_from_file_id / adopted_by_batch_id`，均为可空列，经 `_COLUMN_MIGRATIONS` 添加。`/v1/*` 响应不包含它们。
- **错误格式**：控制台错误使用网关统一的 `{"error": {"type", "code", "message", ...}}` 外层，`code` 为第 2 节列出的代码；422 时附带完整预检报告。错误文案从不包含异常文本或请求内容。
- **未实现/推迟**：与第 1 节一致（试跑、一键重试、CSV/Excel、提示词编辑、数据集库、多模型扫描、精确费用、功能开关、Messages/Responses 创建与复制、内联任务复制）。

### 10.1 第二轮与最终验收修订（实现层面）

- **源文件到期**：提交事务先按 id 加 `FOR UPDATE` 锁，再在新语句中用 `clock_timestamp()` 重新判断状态、删除与到期；不能依赖等待锁之前求值的 `now()`。
- **数字与大小**：超过 Python 整数转换上限的数字按行报 `invalid_json_line`；补全字段与覆盖后的存储请求总大小同样受 `BATCH_MAX_BODY_MB` 限制（`file_too_large`）；派生副本在最后一次写入时触发存储上限也返回 413 `storage_full`。
- **采样参数**：模型目录中的 `supported_sampling_parameters` 以显式列表为准，空列表表示不接受任何采样参数；切换到不接受的模型时，前端可见地清除已设置的覆盖。
- **复制资格**：提交时服务端重新检查 `copied_from` 是文件输入的 Chat/Embeddings 任务（`copy_unsupported`）。
- **前端账号边界**：上传必须显式携带页面所属账号，首次发送、刷新后、重试前及接受结果时都要求令牌属于该账号且会话未被替换；共享客户端增加会话代数，旧会话发起的刷新或 401 重试不能安装旧令牌、结束新会话或以新账号重发。
- **前端过期结果**：上传、识别、校验都带代数与配置快照；更换文件时立即清除旧识别结果，旧结果不能改变页面或成为提交内容。过期意图的「再次提交」「忽略」绑定用户看到的键，并在两种存储方式下比较后再替换或删除。

### 10.2 普通用户的状态分组（产品已确认的简化）

普通用户列表与详情不再直接展示八个 API 状态，而是「全部 + 四类」；`/v1/*` 的 `status`、worker 状态机、存储生命周期和管理员页面的技术状态均不变。

| 类别 | 中文 | 条件（基于显示状态 `display_status`，不读取仍在封存或收尾的文件任务的内部终态） |
|---|---|---|
| `processing` | 处理中 | `validating`、`in_progress`、`finalizing`、`cancelling` |
| `completed` | 已完成 | `completed`，且失败数、取消数为 0，成功数不少于总数 |
| `issues` | 有异常 | `failed`、`expired`（超过执行期限），或 `completed` 但有未成功的请求 |
| `cancelled` | 已取消 | `cancelled`（取消已结束；即使部分请求成功或失败） |

- 规则只有一处定义：`renderers.display_category` 与其 SQL 版本 `batch_store.display_category_sql`，并有对照测试；控制台视图返回 `category` 字段，前端直接使用，不重复推导。
- `GET /user/batches?category=` 在 SQL 中先过滤再分页，保留所有者范围、搜索与 `has_more/last_id`；原 `status=` 精确过滤保留不变（`status=completed` 仍包含部分失败的任务）。非法类别返回 400。
- 徽标旁的简短说明：正在校验数据、正在整理结果、正在取消（进行中的请求会完成）、执行失败、超过执行期限、部分请求失败、所有请求均未成功。暂停仍以单独的警示显示，不作为第五类。结果下载仍以 `results_available` 为准。
