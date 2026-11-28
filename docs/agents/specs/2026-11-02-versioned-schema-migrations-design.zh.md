# 版本化数据库迁移

状态：已实现。操作步骤见 [数据库](../../developer/database.md) 和 [部署](../../developer/deployment.md#applying-schema-migrations)，本文只讲设计和约束。

## 1. 问题与决定

最初，数据库结构没有版本记录，由服务代码在启动时用幂等 DDL 补齐，也有按需执行的（配置加载、配置写入、统计快照）。这带来三个问题：

- `ALTER TABLE` 需要 `ACCESS EXCLUSIVE` 锁，而夜间 `pg_dump` 会在所有表上持有 `ACCESS SHARE` 几个小时。升级后的第一次启动可能落在备份窗口里。
- 为了不让启动卡住，代码只能让服务先跑在没迁移完的结构上，再在后台重试，于是每条依赖新列的代码路径都要处理“列还不在”。这种处理很难测全。
- 回答不了“这个库现在是什么结构”，一次性的数据转换也没地方记“已经做过”。

决定：用 Alembic 维护一条手写的迁移链，迁移是部署中单独的一步，在新代码启动之前执行。服务运行时不执行 DDL，唯一的例外是后台构建的性能索引（5.3）。不引入 ORM，不用 autogenerate，生产环境不执行 downgrade。

## 2. 布局

```text
apps/backend/migrations/
  __init__.py      # 表名常量和 Alembic 配置
  __main__.py      # python -m migrations upgrade | status
  env.py
  compat.py        # 兼容下限的读写和启动检查（第 4 节）
  baseline/        # 冻结的基线建表代码
  versions/        # 0001_baseline.py 起的 revision
```

- 部署执行 `python -m migrations upgrade`。入口自己生成 Alembic 配置，在仓库里和镜像里都能用，不需要 `pyproject.toml`。等不到锁时退出码为 75，表示稍后原样重跑，并列出挡住它的会话。`status` 打印库的版本、兼容下限、代码的 head 和启动时的判断。
- 写新 revision 用 `alembic` 命令，读 `pyproject.toml` 的 `[tool.alembic]`。
- 版本表是 `hybridinference_alembic_version`，不用默认的 `alembic_version`，因为同一个库里可能有别的工具留下的同名表。兼容表 `hybridinference_schema_compat` 只有一行，记录这个库还支持的最老代码版本（第 4 节）。两张表都由 `env.py` 在执行 revision 之前创建，不属于业务结构。
- `env.py` 用 SQLAlchemy async 引擎配 asyncpg，不引入第二个 PostgreSQL 驱动。
- 每个 revision 各用一个事务（`transaction_per_migration=True`），前一个 revision 的表锁在提交时释放。
- 同一个库同时只允许一个迁移进程：`env.py` 先拿会话级 advisory lock，等待上限 60 秒，等不到就报错退出。
- 表锁等待上限 3 秒（`DDL_LOCK_WAIT`）。等不到就失败退出，错误信息里写出持锁的会话（`pg_stat_activity` 里的 pid、`application_name` 和开始时间）。已经在最新版本的库，迁移只查一次版本表，什么都不执行。
- 后端镜像带 `apps/backend/migrations/`，并有 label `org.hybridinference.schema.migrations="alembic"`，部署脚本据此决定是否执行迁移。

## 3. 基线

`0001_baseline` 的 `upgrade()` 把原来启动时的整套建表步骤按原顺序再执行一遍，不把它们抄成 SQL。这些函数原本就是幂等的：先查目录再决定发什么 DDL，锁等待有上限，这些保护都是在生产出过问题之后加的。各部署的结构由各自的升级历史决定，不一定相同，所以“生成一份 SQL 再 `stamp`”的前提无法验证。执行现有函数在新库上建出完整结构，在旧库上补齐缺的部分，然后记录版本号。不需要手动 `stamp`。

这些函数已冻结在 `baseline/` 里，服务代码不再引用。一个测试锁定这些文件和 `0001_baseline.py` 的摘要，ruff 不格式化它们。以后的结构变更只能写成新 revision。

- 基线用独立的 asyncpg 连接，每一步沿用自己的事务和锁等待上限，一步提交一步，不把几十张表的强锁攒到最后。
- 中途失败不写版本号。每一步都幂等，重跑从失败处接着收敛。
- 按功能开关才建的表，基线一律建，库的结构不取决于当时开了哪些功能。
- 基线包含一次性的数据转换：旧角色改名（`internal_group`、`developer` 改为 `internal`，`trial` 改为 `free`）、`api_keys.account_id` 回填、删除旧字段 `api_keys.tier` 和 `api_logs.prompt_hash`、`response_hash`、重建约束。已经跑过当时建表代码的库，这些都不会再发出 DDL 或改数据。不能自动修复的数据（例如重复的广播收件人）会让迁移失败并说明原因，不会悄悄跳过。
- 依赖运行时配置或密钥的操作留在启动流程：按 `ADMIN_EMAILS` 设置管理员、erasure fence 的指纹校验（现在在 `serving.storage.erasure_fence`）。汇总表的缺口回填和大表性能索引是后台任务，不在迁移里。

## 4. 启动检查与兼容判断

新代码启动时读版本表和兼容表，与自己的迁移链比较：

| 库的状态 | 启动时 |
|---|---|
| 没有版本表或版本表为空 | 拒绝，提示执行 `python -m migrations upgrade` |
| 版本等于本版本的 head | 启动 |
| 版本在本版本的迁移链里但不是 head（库比代码旧） | 拒绝 |
| 版本不在迁移链里，兼容下限为空或在迁移链里（库比代码新，但声明仍支持本版本） | 启动，记 warning |
| 版本不在迁移链里，兼容下限也不在（库已不支持这么旧的代码） | 拒绝，报出要求的最低版本 |

“不认识这个版本号”证明不了兼容，所以兼容由库自己记录。每个收缩 revision 在同一个事务里把兼容下限提高到它声明的 revision，扩展 revision 不改，下限只升不降。旧代码只需检查下限是不是自己迁移链里已有的版本。下限为空表示到目前为止所有 revision 都是扩展。

这项检查之前的镜像不检查下限，启动时照旧建表。所以在第一个收缩 revision 之前，部署脚本要加一项检查：镜像不支持迁移而库里已设了兼容下限时，拒绝部署。

拒绝启动会让健康检查失败，对部署失败会自动恢复的发行版，这是漏掉迁移步骤时的兜底，不是发布流程的一部分。

## 5. 以后的结构变更

### 5.1 先扩展，后收缩

迁移执行时旧版本还在服务，部署失败恢复时旧代码会跑在新结构上，所以 revision 默认必须让上一版代码照常工作：加表、加可空列或带默认值的列、加索引。

删列、删表、改名、改类型、收紧约束会让更早的代码失效，要分两个版本做：

1. 先发一版不再使用这个对象的代码，并带一个标记 revision（可以不含任何操作），好让“不再使用”的那一版有可指认的版本号。
2. 下一版写收缩 revision，声明兼容下限就是那个标记 revision。

每个 revision 必须声明 `compatibility = "expand"` 或 `"contract"`。`tests/unit/test_migration_hygiene.py` 要求：`contract` 声明兼容下限，`expand` 不声明；标成 `expand` 却含明显收缩操作的会被拒绝；只有一个 head；编号连续不重复；revision 不引用 `serving.*`；基线内容被锁定。已在共享库上执行过的 revision 不能修改，只能再写新 revision 往前修。

### 5.2 大表索引

大表（例如 `api_logs`）上的索引可能要建几个小时，`CREATE INDEX CONCURRENTLY` 还要等所有更早的事务结束，备份也算在内。这类索引不放进阻塞部署的迁移，继续在后台构建（`storage/perf_metrics_index.py`）。规则：运行时只允许影响性能的对象（索引）暂时缺失，影响正确性的对象（表、列、约束、触发器）必须由迁移保证。

### 5.3 数据修补

有边界、跑得快的修补写成数据 revision，放在对应的结构 revision 之后，各用一个事务。可能很慢的修补（例如扫大表）做成后台任务，完成情况要持久记录，不能每次启动都重跑。

## 6. 部署约定

1. 按 digest 拉取新镜像。
2. 用这个镜像执行 `python -m migrations upgrade`，此时旧版本还在服务。必须是同一个 digest，不能按 tag 另拉。
3. 迁移成功后用同一个镜像重启服务。迁移失败就中止，旧版本继续服务。

- 普通启动和回滚都不运行迁移。不要把迁移做成服务的启动依赖（例如 compose 的 `depends_on: condition: service_completed_successfully`）：恢复路径通常是用当前 compose 文件配上一版镜像，这个依赖会让旧镜像也去迁移，而旧镜像的迁移链里没有新的版本号，Alembic 报 “Can't locate revision” 退出，恢复就失败了。
- 没有 label 的镜像（这项能力之前的版本）跳过迁移，照旧自己建表，所以部署脚本的改动可以先于镜像上线。
- 含 DDL 的版本遇到长时间持锁的任务（例如夜间备份）时，部署在动服务之前就失败。避开这个窗口发布。
- 上游示例：compose 用 `backend` 服务自己的定义起一次性容器 `docker compose run --rm backend python -m migrations upgrade`，`make migrate` 就是它，`make up` 和 `make build` 在启动服务前先执行。`backend` 固定了网络地址的 compose 文件要另设一个迁移服务（镜像和 `DB_*` 相同、不设固定地址、用 profile 挡在 `up` 之外），否则一次性容器会因地址被占用而创建失败。systemd：重启服务前先执行 `.venv/bin/python -m migrations upgrade`，服务单元不加 `ExecStartPre`。`DB_ENABLED=false` 时命令什么都不做，部署脚本可以无条件执行。

## 7. 服务代码里已删除的补偿机制

由于 `ALTER` 不再发生在启动时，且 4 节的检查保证新代码只在版本到位时启动，下面这些已删除：后台重试迁移，`AccountSchema` 按列拼 SQL 与 `AccountSchemaPending` 的 503，`RequiredOperationalSchemaUnavailable`，batch 的“迁移完成前不可用”，使用量汇总的“列未迁移时现算”，以及各处按需建表检查。`SchemaLockUnavailable` 只留在基线和迁移里，用来让迁移失败退出。

密码重置工具（`serving.auth.reset_password`）仍按目录里实际存在的列读取，因为它是管理员被锁在外面时的恢复手段，要在迁移等锁、旧版本还在服务时也能用。

## 8. 验证

- 空库升级到 `0001_baseline` 的结构，与签入的结构快照一致。旧库升级后与空库迁移的结果一致；历史数据（旧角色、NULL 的 `account_id`、旧字段、旧约束）转换正确。
- 基线每一步注入失败或另一个会话持锁时，迁移失败、不写版本号，重跑后收敛。已收敛的库在所有表被 `ACCESS SHARE` 锁住时执行基线，也照常完成。
- 两个迁移进程同时启动，一个执行，另一个超时退出。
- 用上一个镜像连接迁移后的库，能启动并完成登录、创建 API Key、带 Key 的请求和日志写入、管理员查用户。第 4 节表的每一行都有单元测试。
- 静态扫描 `serving/`，除后台索引构建外不允许 DDL；另有一个专用测试库在迁移后装 DDL 事件触发器，走一遍原来会建表的运行时路径，出现 DDL 即失败。
- 测试库用迁移建库，不再调用各 store 的 `initialize()`。

发布前可对目标库做只读检查：专用表名未被占用，旧角色、`trial` 可见性覆盖、待回填的 Key 和旧字段计数都应为 0；不为 0 的就是基线会改的数据。
