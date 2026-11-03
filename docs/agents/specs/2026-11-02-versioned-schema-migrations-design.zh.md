# 版本化数据库迁移：用 Alembic 取代运行时建表

- 日期：2026-11-02
- 状态：草案，待评审。设计与实现放在同一个 PR 里，一次发布完成；§12 列出需要评审拍板的默认决定
- 范围：`apps/backend/serving/` 中所有建表、改表的代码，启动流程里的 schema 步骤，后端镜像，
  上游自带的部署示例（`deploy/docker/`、`deploy/systemd/`、`distributions/example/`），
  以及发行版部署脚本需要满足的约定（§6）。发行版自己的脚本由发行版修改
- 取代：[2026-05-03 Alembic 迁移设计](2026-05-03-alembic-migrations-design.md)（草案，未实施）。
  §13 逐条说明不同之处和原因
- 相关记录：#1551（provider-stats 回填改为每次启动执行）、
  [测试数据库契约](2026-10-18-test-database-contract-design.zh.md)

## 1. 背景

### 1.1 现状（基于 `dev` @ `73c5675b`）

数据库结构没有版本记录，全靠代码里的幂等 DDL 在运行时补齐：

- `apps/backend/serving/` 下 17 个模块共有 255 条 `CREATE TABLE/INDEX ... IF NOT EXISTS` 和
  `ADD COLUMN IF NOT EXISTS`。在空库上建出来是 70 张表、168 个索引、101 个约束。
- 大部分在启动时执行（`serving/servers/bootstrap.py` 的 lifespan），也有一些按需执行：配置首次加载
  （`app_config.load`）、配置写入（`app_config._writable_store`）、公开统计快照的计算和写入
  （`public_stats.compute_snapshot`、`store_snapshot`）都会先跑一遍建表检查。
- 同一张表不止一份定义。`users`、`api_keys`、`auth_sessions` 等 6 张表在 `storage/database.py` 和
  `storage/postgres_operational.py` 里各有一份 `CREATE TABLE`，连带的数据转换也各写了一份。其中 `users`
  的两份定义已经不一样了，后者多 6 列，最终靠后面的补列语句收敛到同一个结构。
- 建表函数里还夹带着数据转换、旧字段删除和依赖运行时配置的写入，§4.3 逐项列出。

### 1.2 启动时改表带来的补偿机制

改表要拿锁。`ALTER TABLE` 需要 `ACCESS EXCLUSIVE`，而部署方的夜间 `pg_dump` 会在所有表上持有
`ACCESS SHARE`，一持就是几个小时。升级后的第一次启动完全可能落在备份窗口里。为了不让启动卡住、
健康检查超时，代码做了这几件事：

1. 每条迁移的锁等待都有上限，等不到就抛 `SchemaLockUnavailable`（`storage/log_schema.py`），
   9 个模块里都有对它的处理。
2. 启动时捕获这个异常，把迁移交给后台按指数退避重试（`bootstrap._schedule_deferred_schema_migration`），
   服务先在旧结构上运行。
3. 既然服务可能跑在没迁移完的结构上，查询就得先看表里有哪些列。`AccountSchema` 按目录里实际存在的列
   拼 SQL，缺的列按默认值读；需要缺失列的写操作抛 `AccountSchemaPending`，接口返回 503
   （`storage/account_schema.py`）。
4. 有些列缺了就不能安全运行，于是又有一份"必须先有"的清单，缺了就拒绝启动
   （`RequiredOperationalSchemaUnavailable`）。
5. batch 的结构没迁移完时，batch 推理整体不可用，直到后台重试成功。

在"启动时改表"这个前提下，这套机制是必要的，设计得也很仔细。问题出在前提本身：只要代码可能跑在
迁移了一半的结构上，每条依赖新列的代码路径都得自己处理"列还不在"的情况，而这种处理很难测全。

### 1.3 没有版本记录的其他代价

- 回答不了"这个库现在是什么结构"。每个部署的结构由它自己的升级历史决定，只能连上去查目录。
- 一次性的数据转换没有地方记"已经做过"，只能每次启动都重新检查一遍。§4.3 里的旧角色转换、
  API Key 归属回填都是这样。

### 1.4 五月的方案

2026-05-03 写过一版 Alembic 方案，状态一直停在草案。之后代码变化不小，那版的几个前提已经不成立，
§13 逐条对比。

## 2. 目标与非目标

目标：

1. 表结构有一条版本化的迁移链，每个数据库都记录自己迁移到了哪一版。
2. 迁移是部署中单独的一步，在新代码启动之前执行。服务运行时不执行 DDL，唯一的例外是后台构建的
   性能索引（§5.3）。
3. 现有数据库（包括各发行版结构不尽相同的库）不需要手动 `stamp`，执行一次升级就能收敛到基线。
4. 回滚安全：部署失败时要恢复的旧镜像能不能跑在新结构上，由明确的兼容规则判断（§7），
   并用那个镜像实际验证（§9、§10）。
5. 同一次发布删掉 §1.2 的补偿机制。

非目标：

- 不引入 ORM，也不用 autogenerate，迁移手写。
- 不改汇总表的缺口回填，它们是持续运行的任务。
- 大表上的索引不放进阻塞部署的迁移，见 §5.3。
- 生产环境不执行 Alembic 的 downgrade，见 §5.1。
- 不清理遗留表（例如 DR5 保留的 `agent_*` 表）。以后需要时写成普通 revision。

## 3. 布局与配置

```text
apps/backend/migrations/
  __init__.py      # 表名常量，以及不依赖目录布局的 Alembic 配置
  __main__.py      # python -m migrations upgrade | status
  env.py
  script.py.mako
  compat.py        # 兼容下限的读写和启动检查，见 §7
  baseline/        # 冻结的基线建表代码，见 §4
  versions/
    0001_baseline.py
```

- 部署执行 `python -m migrations upgrade`。这个入口自己生成 Alembic 配置，`script_location` 取包所在的
  目录，所以在仓库里（`apps/backend` 在 `sys.path` 上）和镜像里（包和 `serving` 一起放在 `/app` 下）
  都能直接用，镜像里不需要 `pyproject.toml`。锁等不到时退出码是 75，表示稍后原样重跑即可，并列出持锁的
  会话。`status` 打印库的版本、兼容下限、代码的 head 和 §7 的判断。
- 仓库里写新 revision 用 `alembic` 命令，它读 `pyproject.toml` 的 `[tool.alembic]`（Alembic 1.16 起
  支持），不另建 `alembic.ini`。
- 版本表用专用的名字 `hybridinference_alembic_version`，不用默认的 `alembic_version`。同一个数据库里
  可能有别的工具或历史版本留下的 `alembic_version`，而从仓库这一侧没法确认每个部署都没有。
- 兼容表 `hybridinference_schema_compat` 只有一行，记录这个库还支持的最老代码版本（§7）。它和版本表
  一样属于迁移的元数据，由 `env.py` 在执行任何 revision 之前创建，不属于业务结构。
- `env.py` 用 SQLAlchemy 的 async 引擎配 asyncpg（Alembic 官方的 async 模板），不引入第二个 PostgreSQL
  驱动。Alembic 本身依赖 SQLAlchemy，这是新增的依赖。
- 每个 revision 各用一个事务（`transaction_per_migration=True`），前一个 revision 拿的表锁在它提交时就释放，
  不会一路带进后面的数据修补。Cloud Agent 在这里踩过坑。
- 迁移进程互斥：`env.py` 先在 Alembic 的连接上拿一个会话级 advisory lock，再执行 revision。等待有上限，
  等不到就报错退出，说明另一个迁移正在运行。
- 后端镜像的最终阶段目前只拷贝 `serving/`、`routing/`、`config/`，需要再把 `apps/backend/migrations/`
  拷到 `/app/migrations/`，并加一个 label 标明镜像支持迁移（§6.2）。迁移命令是在 `/app` 下执行
  `python -m migrations upgrade`。

## 4. 基线：收敛，并在这次冻结

### 4.1 做法

`0001_baseline` 的 `upgrade()` 把今天启动时执行的那一整套建表步骤按同样的顺序再执行一遍，不把那
255 条语句重新抄成 SQL。原因有两个：

1. 现有的建表函数都是幂等的，大多数先查目录再决定发什么 DDL，锁等待也有上限。机械地抽成 SQL 会丢掉
   这些保护，而它们都是在生产上出过问题以后才加上的。
2. 各部署的结构由各自的升级历史决定，不一定完全相同。"按当前代码生成一份 SQL，再对现有库执行 `stamp`"
   的前提是所有库都和这份 SQL 一致，这一点无法验证。执行现有函数则正是每次重启都在做的事：新库建出
   完整结构，旧库补上缺的部分。补完再记录版本号。

这些函数在这次就冻结：原样搬进 `apps/backend/migrations/baseline/`，去掉其中的运行时工作（§4.3），
服务代码不再引用。从此它们只属于基线，以后的结构变更只能写成新的 revision。一个测试锁定这些文件和
`0001_baseline.py` 的摘要；ruff 不格式化这几个文件，以免格式化工具升级时改写历史。
`users` 等表的重复定义也随之只存在于这份冻结的历史里；执行顺序保持不变，建出的结构和今天一样。

### 4.2 实现要点

- 用独立的 asyncpg 连接执行，不复用 Alembic 的连接。各步骤沿用自己的事务和锁等待上限（`log_schema`
  的 3 秒），一步提交一步，不会把几十张表的强锁攒到最后一起释放。
- 基线中途失败时不写入版本号。每一步都是幂等的，重跑会从失败的地方接着收敛。
- 启动流程里按条件执行的步骤（比如某个功能开启时才建的表），基线一律执行。库的结构不应该取决于当时
  开了哪些功能。
- 锁等不到（`SchemaLockUnavailable`）时，基线直接失败退出，错误信息里写出挡住它的会话（§5.2），
  不做后台重试。

### 4.3 数据边界

建表路径上除了纯结构的 DDL，还有下面这些操作，逐项决定归属：

| 操作 | 位置 | 归属 | 理由 |
|---|---|---|---|
| `users.role` 设默认值 `'free'`、回填 NULL、设 NOT NULL | `database.py`、`postgres_operational.py` 各一份 | 基线 | 一次性转换。NOT NULL 之后不会再出现 NULL |
| 旧角色转换：`internal_group`、`developer` 改为 `internal`，`trial` 改为 `free`；重建 `users_role_check` | 同上 | 基线 | 一次性转换。只在约束还不是当前四个角色时执行，新约束阻止旧值再写入 |
| `model_visibility_overrides` 中的 `trial` 改为 `free` | `postgres_operational.py` | 基线 | 一次性转换。`trial` 已不是合法角色，接口写不进来 |
| `api_keys.account_id` 为 NULL 时回填为 `user_id` | 两份 | 基线 | 一次性转换。现在创建 Key 时总会写 `account_id`（缺省取 `user_id`），不会再产生新的 NULL |
| 删除 `api_keys.tier` | 两份 | 基线 | tier 与 role 合并后留下的旧字段，当前代码不再读写 |
| 删除 `api_logs.prompt_hash`、`response_hash` 及其索引 | `log_schema.py` | 基线 | 很早以前的旧字段，只在仍然存在时执行 |
| 删除索引 `idx_disabled_provider_env_keys_provider` | `postgres_operational.py` | 基线 | 多余的索引，只影响性能 |
| 重建 `users_status_check`、`phone_auth_codes_purpose_check`，把 `signup_allowed_domains_created_by_fkey` 改为 `ON DELETE SET NULL`，给 `email_broadcast_recipients` 加唯一约束，去掉 `users.email` 和 `login_events.email` 的 NOT NULL | 多处 | 基线 | 结构收敛，都先查目录，已收敛的库不执行。唯一约束遇到重复数据会失败，见 §9 |
| 固定行：`admin_view_state` 的单行、`config_source_revisions` 的初始记录 | `log_schema.py`、`config/revisions.py` | 基线 | 结构要求的行，`ON CONFLICT DO NOTHING` |
| 按 `ADMIN_EMAILS` 把用户设为管理员 | 两份 | 启动流程 | 读运行时配置（其中包括数据库里的配置），每次启动都要对新注册的用户生效。迁移不应依赖运行时配置 |
| fence 指纹的校验与初始化（`validate_or_init_fingerprint`） | `database.py`、`log_schema.py` | 启动流程 | 依赖运行时密钥 |
| `backfill_token_columns` 和各汇总表的缺口回填 | `admin/*_rollup.py` 等 | 后台任务，不变 | 只改汇总表，范围有界，不阻塞启动 |
| 大表上的性能索引 | `perf_metrics_index.py` | 后台任务，不变 | 见 §5.3 |

留在启动流程的两项都是普通的数据读写，不含 DDL。它们从冻结的基线代码里删掉，由启动流程单独调用，
各只保留一份。

### 4.4 对已经在运行的库会做什么

- 如果一个库最近一次启动运行的，就是基线冻结时那一版的建表代码，那么表 4.3 里归入基线的每一项都已经
  执行过：先查目录的步骤不会再发出 DDL，带条件的 UPDATE 也找不到要改的行。基线在这样的库上的实际效果，
  是多出版本表和兼容表。
- 部署方运行的版本如果更早，两版之间建表代码的差异，就是基线在它的库上会额外做的事。发布前要把这份
  差异列出来（§10.2）。
- 这个结论要用数据核实，不能只靠推理。发布前在目标库上执行附录里的只读查询：旧角色、`trial` 可见性
  覆盖、待回填的 Key、旧字段都应该是 0。不为 0 的项，就是基线会改的数据。

## 5. 以后的结构变更怎么写

### 5.1 先扩展，后收缩

- 迁移执行时旧版本还在服务；部署失败恢复时，旧代码会跑在新结构上。所以 revision 默认必须让上一版
  代码照常工作：加表、加可空列或带默认值的列、加索引，一步完成。
- 删列、删表、改名、改类型、收紧约束会让更早的代码失效，要分两个版本做：
  1. 先发一版不再使用这个对象的代码。这一版要带一个标记 revision，可以不含任何操作。
  2. 下一版再写收缩 revision，并在其中声明兼容下限就是那个标记 revision（§7）。
- 标记 revision 的作用，是让"已经不再使用"的那一版代码有一个能被指认的版本号。没有结构变化的几次发布
  共用同一个 head，不加标记就分不清哪一版还在用旧对象。
- 每个 revision 都要声明 `compatibility = "expand"` 或 `"contract"`，由写的人判断；正则列不全会让旧代码出错的
  写法（比如删掉一个 `ON CONFLICT` 依赖的唯一索引）。迁移规范测试要求：`contract` 必须声明兼容下限，`expand`
  不能声明；标成 `expand` 却含有明显收缩操作的，测试也拒绝。模板里这一行默认是 `None`，不填测试就不过。
- 已经在共享库上执行过的 revision 不能修改或撤销，只能再写一个新的 revision 往前修。
- 生产环境不执行 downgrade。revision 可以写 downgrade，供开发和测试使用。

### 5.2 锁等待有上限，失败时说清原因

- 沿用 `log_schema` 现有的锁等待上限（`bounded_ddl` / `execute_ddl`，3 秒）。等不到锁就失败退出，
  错误信息里写出挡住它的会话：从 `pg_stat_activity` 查出 pid、`application_name` 和开始时间，
  例如"被 pg_dump 持有，开始于 …"。
- 部署方如果有长时间持锁的任务（比如夜间备份），应避开这个窗口发布含 DDL 的版本。不含 DDL 的版本
  不受影响：库已经在最新版本时，迁移步骤只查一次版本表，什么都不执行。

### 5.3 大表上的索引

- 大表（例如 `api_logs`）上建索引可能要几个小时。`CREATE INDEX CONCURRENTLY` 不阻塞写入，但要等所有更早的
  事务结束，正在跑的备份也包括在内。现有的 `storage/perf_metrics_index.py` 就是在后台构建，超时设为 12 小时。
- 这类索引不放进阻塞部署的迁移，继续在后台构建。规则是：运行时只允许影响性能的对象（索引）暂时缺失，
  不允许影响正确性的对象（表、列、约束、触发器）缺失，后者必须由迁移保证。

### 5.4 一次性的数据修补

- 有边界、跑得快的修补写成数据 revision，放在对应的结构 revision 之后，各自一个事务。
- 可能很慢的修补（比如要扫大表）不放进部署，改成后台任务，但完成情况要持久记录，不能每次启动都重跑。

## 6. 部署：迁移是单独的一步

### 6.1 顺序

1. 拉取新镜像，按 digest 固定。
2. 用这个镜像执行 `python -m migrations upgrade`。此时旧版本还在服务。
3. 迁移成功后，用同一个镜像重启服务。迁移失败就中止部署：服务不切换，旧版本继续服务。

### 6.2 执行约束

- 同一个数据库同一时间只有一个迁移进程。部署脚本自己的并发控制之外，再由 §3 的 advisory lock 兜底。
- 普通启动和回滚都不运行迁移。不要把迁移做成服务的启动依赖（比如 compose 的
  `depends_on: condition: service_completed_successfully`）。恢复路径通常是用当前的 compose 文件配上
  上一版镜像重新启动服务，这个依赖会让旧镜像也去执行迁移。旧镜像的迁移链里没有新的版本号，Alembic 会报
  "Can't locate revision" 并退出，恢复本身也就失败了。
- 执行迁移的镜像必须就是这次要部署的镜像，即同一个 digest，不能按 tag 另拉一份。
- 镜像用 label 标明自己支持迁移。部署脚本据此决定是否执行迁移：没有这个 label 的镜像（这次发布之前的
  版本）跳过迁移，照旧由它自己在启动时建表。这样，部署脚本的改动可以先于新镜像上线，对现有镜像没有影响。

### 6.3 发行版的发布前提

第一个带迁移的镜像部署之前，发行版的部署脚本必须已经支持 §6.1 和 §6.2，并且已经在一次真实部署里确认
生效，比如日志里出现"镜像不支持迁移，跳过"。

不能指望启动检查加自动恢复来兜底。新容器启动失败、再恢复旧容器，中间仍然有一段服务中断，恢复本身也
需要演练。

### 6.4 上游自带的部署示例

- compose：迁移用 `backend` 服务自己的定义起一个一次性容器，
  `docker compose run --rm backend python -m migrations upgrade`，不另设服务。这样迁移用的一定是服务
  马上要用的那个镜像和 `DB_*` 设置，按 digest 部署的覆盖层（`docker-compose.digest-backend.yml`）也一样；
  它不是 `backend` 的依赖，普通的 `up -d` 和恢复都不会运行它。`make migrate` 就是这条命令，`make up`
  和 `make build` 在启动服务之前先执行它。
  例外是 `backend` 固定了网络地址的 compose 文件，比如 FreeInference 的：一次性容器会沿用这个地址，而它
  正被运行中的 backend 占着，Docker 拒绝创建（`Address already in use`）。这种部署要单独定义一个迁移服务，
  镜像和 `DB_*` 与 backend 相同，不设固定地址，用 profile 挡在 `up` 之外。部署文档写明了这一条。
- `deploy/systemd/`：部署说明里，重启服务前先在工作目录执行 `.venv/bin/python -m migrations upgrade`；
  服务单元不加 `ExecStartPre`，否则每次重启都会迁移。
- `distributions/example/`：第一阶段没有数据库，不迁移；`make demo` 先启动 PostgreSQL、执行迁移，再启动
  带数据库的后端。没有数据库（`DB_ENABLED=false`）时这条命令什么都不做，部署脚本可以无条件执行它。

## 7. 启动检查与兼容判断

新代码启动时读版本表和兼容表，与自己的迁移链比较：

| 库的状态 | 含义 | 启动时 |
|---|---|---|
| 没有版本表，或版本表为空 | 还没执行过迁移 | 拒绝启动，提示执行 `python -m migrations upgrade` |
| 版本等于本版本的 head | 正常 | 启动 |
| 版本在本版本的迁移链里，但不是 head | 库比代码旧，迁移没执行 | 拒绝启动 |
| 版本不在本版本的迁移链里；兼容下限为空，或在本版本的迁移链里 | 库比代码新，且库声明仍然支持这一版代码 | 启动，记一条 warning |
| 版本不在本版本的迁移链里，兼容下限也不在 | 库比代码新，且已经不支持这么旧的代码 | 拒绝启动，报出要求的最低版本 |

关键在第四行。"不认识这个版本号"本身证明不了兼容，所以兼容与否由库自己记录：

- 每个收缩 revision 在同一个事务里，把兼容下限提高到它声明的那个 revision（§5.1）。扩展 revision 不改它。
  下限只升不降。
- 旧代码不需要知道以后的 revision，只要查兼容下限是不是自己迁移链里已有的版本。
- 兼容下限为空，表示到目前为止所有 revision 都是扩展。这由迁移规范测试保证（`contract` revision
  必须声明下限），每次发布时再由 §9 用旧镜像实际检查。

这次发布之前的镜像没有这项检查，启动时照旧建表。在第一个收缩 revision 合并之前，发行版的部署脚本要
加上一项检查：如果要部署的镜像不支持迁移，而库里已经设置了兼容下限，就拒绝部署。在那之前，基线和
扩展 revision 都不影响 #1704 及之后的旧镜像。#1704 之前的镜像会把基线删掉的唯一索引建回来，见 §10.2
第 4 步。

拒绝启动会让健康检查失败。对部署失败会自动恢复的发行版来说，漏掉迁移步骤的部署会退回旧版本，而不是
带着不匹配的结构运行。但这只是兜底，不是发布流程的一部分（§6.3）。

## 8. 删除补偿机制

同一次发布里删除：

- 后台重试：`_schedule_deferred_schema_migration`，以及服务代码里各处对 `SchemaLockUnavailable` 的处理；
- `AccountSchema` 按列拼 SQL、`AccountSchemaPending` 和对应的 503，连同手机登录路由的
  `require_phone_accounts` 检查和头像列缺失时的兜底；
- `RequiredOperationalSchemaUnavailable`；
- batch 的"迁移完成前不可用"占位，以及注销账号时为它保留的 `batch_store_pending`；
- 使用量汇总表在"列还没迁移完"时改从 `api_logs` 现算的兜底（`user_usage_rollup.stored_ready`）；
- 告警历史第一次取连接时的建表和表存在检查；
- 按需执行的建表检查（配置加载、配置刷新、配置写入、公开统计快照）。

建表代码都搬走以后，`serving.storage.log_schema` 只剩 erasure fence 和指纹校验，改名为
`serving.storage.erasure_fence`。

保留一处按列读取：密码重置工具（`serving.auth.reset_password`）。它是管理员被锁在外面时用的恢复手段，
恰恰要在迁移等锁、旧版本还在服务的时候也能用，所以仍然先查目录，只读存在的列，不改结构。

这些机制的前提，是代码可能跑在没迁移完的结构上。§7 的检查保证新代码只在版本到位时启动，这个前提不再
成立。`SchemaLockUnavailable` 本身留在基线代码和迁移里，用来让迁移失败退出。

## 9. 测试与验收

以下验收项全部在这个 PR 里完成：

1. **空库能建好。** 只升级到 `0001_baseline` 得到的结构（表、列、类型、默认值、索引、约束、触发器、
   函数），和一份签入的结构快照完全一致。快照在第一个提交里由当时的启动流程生成，所以后面删掉启动建表
   以后，这项测试照样有对照物。
2. **旧库能升级。**
   - 结构：用上一个 dev 镜像建库，执行迁移，结构与空库迁移的结果一致。
   - 历史数据：空库只能验证结构，所以要人工构造旧结构和旧数据，包括 NULL 角色，`trial`、`developer`、
     `internal_group` 角色，`trial` 可见性覆盖，`account_id` 为 NULL 的 Key（对应用户存在和不存在两种），
     带 `tier` 列的 `api_keys`，带 `prompt_hash` 的 `api_logs`，旧版约束，以及 `email` 仍为 NOT NULL 的
     `users`。执行迁移后逐项检查转换结果。
   - 现有的 `tests/servers/test_role_migration.py`、`tests/servers/test_account_id_backfill.py`、
     `tests/integration/storage/test_phone_auth_codes.py`、`tests/unit/storage/test_account_schema.py`
     改为针对基线运行。
   - 迁移无法自动修复的数据（比如重复的广播收件人），要验证迁移会失败并说明原因，而不是悄悄跳过。
3. **中途失败能重试。** 在基线的每一步注入失败，或者让另一个会话持有表锁。迁移应当失败退出，报出挡住
   它的会话，不写入版本号；重跑后收敛到同样的结构。另一面：一个已经收敛的库，在所有表都被
   `ACCESS SHARE` 锁住（相当于 `pg_dump` 正在运行）时执行基线，应当照常完成，因为没有一步需要强锁。
   这正是已部署的库第一次迁移时的情形。
4. **同时只有一个迁移。** 两个迁移进程同时启动：一个执行，另一个等待超时后报错退出，库的状态正确。
5. **旧镜像能恢复服务。** 用上一个 dev 镜像连接迁移后的库，确认它能启动并完成关键读写：登录、创建 API
   Key、用 Key 发一次请求并写入请求日志、管理员查询用户列表。这项检查进 CI。§7 表格的每一行都有单元测试。
6. **业务代码不再建表。** 两道检查：
   - 静态扫描 `serving/`，除了后台索引构建之外不允许出现 DDL；
   - 一个专用的测试库在迁移完成后装上 DDL 事件触发器，再走一遍以前会建表的运行时路径（启动初始化、
     配置的加载和写入、公开统计快照）：其间出现任何 DDL 都会报错。不装在共享测试库上，是因为有 33 个
     测试文件要自己执行 DDL 来构造旧结构。
7. **迁移规范**（参照 Cloud Agent 的 `tests/unit/test_migration_hygiene.py`）：只有一个 head；revision
   编号连续且不重复；revision 不引用 `serving.*`；基线内容被锁定；每个 revision 声明 expand 或 contract，contract 必须声明兼容下限。
8. **测试库改用迁移建库**，fixture 不再调用各 store 的 `initialize()`。

## 10. 实施与发布

### 10.1 一个 PR，按顺序分提交

1. 接入 Alembic：依赖、`migrations/`、`[tool.alembic]`、版本表和兼容表、冻结的基线、结构快照和等价测试。
   此时启动流程还没动，正好用来生成快照。
2. 旧库、历史数据、失败重试和并发测试。
3. 测试库改用迁移建库。
4. 启动流程：删除建表步骤（包括按需执行的），换成 §7 的检查；单独调用留在启动流程的两项；删除补偿机制
   （§8）；加上 DDL 绊线；现有的转换测试改为针对基线。补偿机制依赖的目录查询函数随建表代码一起搬走，
   所以这两件事放在同一个提交里。
5. 镜像（拷贝目录、加 label）、上游部署示例、文档和发布说明；旧镜像检查进 CI。

每个提交都要让测试全部通过，评审可以一个提交一个提交地看。

### 10.2 发布顺序

1. 发行版先合并部署脚本的改动（§6），并在一次真实部署里确认生效（§6.3）。
2. 合并本 PR。staging 部署它时，是迁移第一次真正执行。
3. 在 staging 上确认版本表在 head、关键路径正常，然后演练恢复：用 #1704 之后、本 PR 之前的一个镜像
   （生产完成第 4 步的第一步之后跑的就是这一类）连接迁移后的库，确认能启动并完成关键读写，再前进回来。
   不要用 #1704 之前的镜像演练，原因见第 4 步。
4. 上生产分两次部署：
   1. 还在跑 #1704 之前版本的部署，先升到 #1704 之后、本 PR 之前的版本，确认正常。这一步由旧的启动
      建表代码删掉那两个"一个账号只能一把 key"的唯一索引。
   2. 再部署本 PR。部署前：列出生产所跑版本与基线冻结点之间建表代码的差异；在生产库上执行附录的只读
      检查；避开长时间持锁的任务（比如夜间备份）。

   为什么要分开：部署失败时退回的是上一个镜像，它会在迁移后的库上执行自己的启动建表代码。#1704 之前的
   镜像会把那两个索引建回来，而迁移已经记为完成，下一次部署不会再删，之后建第二把 key 就报错。如果那时
   已经有账号持有两把有效 key，索引建不成，旧镜像重试 3 次后会在没有数据库的情况下启动，登录和 API key
   校验都不可用。分两次部署后，本 PR 的上一个镜像已经在 #1704 之后，它的启动建表代码和基线一致。

## 11. 对现有部署的影响

| | 这次发布 |
|---|---|
| 第一次迁移 | 执行基线。对运行着冻结点那版建表代码的库，效果是多出版本表和兼容表（§4.4），用附录的查询核实 |
| 运行中的服务 | 启动不再改表，多一次版本检查；迁移期间不会再出现 503 降级 |
| 部署流程 | 必须先执行迁移。含 DDL 的版本遇上长时间持锁的任务时，部署在动服务之前就失败 |
| 回滚 | 按 §7 判断。这次发布没有收缩 revision，之前的镜像可以照常恢复，前提是它在 #1704 之后（§10.2 第 4 步） |
| 发行方 | 部署流程上的破坏性变化，需要发布说明；启动检查的报错写清该执行什么命令 |

## 12. 风险与待定问题

- PR 很大。按 §10.1 分提交评审；等价测试在第一个提交里就位，保护后面的搬迁。
- 冻结点之后、合并之前，dev 上如果又有结构变更，要并入基线并重新生成快照，不能另写成 0002。基线要等于
  合并前最后一版启动建表代码；它若落后，会在已经跑过新代码的库上把新代码删掉的东西加回来。11 月 3 日并入
  dev 时遇到了这种情况：#1704 删掉了两个“每个账号只能有一把 key”的唯一索引，并新增了 `api_keys.name`；
  #1715 新增了 `batch_jobs.cache_read_tokens`。旧基线还会建那两个索引：在 #1704 已经部署过的库上，索引会被
  重新加回来；如果某个账号已经有两把 key，建索引直接失败。三处变更都已并入基线，#1704 为半迁移结构写的
  运行时退路也一并删除。同一天再次合并 dev 时，又并入了 #1706 新增的 `api_key_daily_cost` 表。并入之后，
  空库迁移的结果与 dev（e7f14208）启动建出的结构一致：71 张表、168 个索引、102 个约束；用旧建表代码
  （73c5675b）建的库迁移后也一致。
- 这次发布之前的镜像不检查兼容下限（§7）。第一个收缩 revision 之前，要补上部署脚本的那项检查。
- 需要评审拍板的默认决定：
  1. 版本表名 `hybridinference_alembic_version`，兼容表名 `hybridinference_schema_compat`；
  2. async 引擎配 asyncpg，而不是引入 psycopg；
  3. 表锁等待上限沿用 `log_schema` 的 3 秒，迁移进程互斥锁的等待上限默认 60 秒；
  4. 镜像 label 的名字，默认 `org.hybridinference.schema.migrations=alembic`。

## 13. 与 2026-05-03 方案的差异

| | 五月方案 | 本文 | 原因 |
|---|---|---|---|
| 基线 | 把当时的 `CREATE` 语句抽成 SQL，现有库手动 `alembic stamp` | 冻结现有的幂等建表函数，执行一次升级自动收敛 | 建表代码带目录检查、锁等待上限和数据转换，各部署的结构也不一定相同 |
| 启动检查 | 版本必须严格相等 | 库比代码旧时拒绝；库比代码新时按库里记录的兼容下限判断 | 部署失败用旧镜像恢复时，旧代码要能在新结构上启动，但"不认识"不能当成"兼容" |
| 切换方式 | 一个 PR 同时接入 Alembic 并删除启动建表 | 同样一次完成，但部署脚本的支持要先于镜像上线，并且要演练恢复 | 迁移第一次在生产上执行，发布顺序决定会不会中断服务 |
| 数据转换 | 未涉及 | 逐项审计，分别归入基线或启动流程（§4.3） | 建表函数里夹带着角色转换、Key 归属回填和旧字段删除 |
| 大表 DDL | 不在范围内 | 明确规则：只有索引可以在后台构建，其他对象必须由迁移保证 | 生产规模下后台建索引已经是现状 |
| 部署脚本 | 修改 `scripts/deploy_*.sh` | 上游只改自带的示例，发行版按 §6 的约定改自己的脚本 | 这些脚本已经移到各发行版 |
| 驱动 | 同步 SQLAlchemy 引擎 | async 引擎配 asyncpg | 不引入第二个驱动 |
| 版本表 | `alembic_version` | 专用表名 | 避免和库里其他工具的版本表冲突 |
| D1 | D1 继续用 `d1_schema.sql` | 不再相关 | D1 后端已经移除 |

## 附录：发布前的只读检查

在目标库上执行，全部是只读查询：

```sql
-- 专用表名没有被占用：两列都应为 NULL
SELECT to_regclass('hybridinference_alembic_version'), to_regclass('hybridinference_schema_compat');

-- 基线会转换的数据：都应为 0
SELECT count(*) FROM users WHERE role IS NULL OR role NOT IN ('free', 'pro', 'internal', 'admin');
SELECT count(*) FROM model_visibility_overrides WHERE required_role = 'trial';
SELECT count(*) FROM api_keys k
WHERE k.account_id IS NULL AND EXISTS (SELECT 1 FROM users u WHERE u.id = k.user_id);

-- 基线会删除的旧字段：应返回 0 行
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema = current_schema()
  AND (table_name, column_name) IN (('api_keys', 'tier'), ('api_logs', 'prompt_hash'), ('api_logs', 'response_hash'));
```

约束和索引层面的差异，用目标库的 schema-only 导出与 staging 迁移后的导出比较。
