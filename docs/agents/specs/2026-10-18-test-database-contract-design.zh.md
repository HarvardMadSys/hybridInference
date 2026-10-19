# 测试数据库契约设计：一个入口、一种失败方式

- 日期：2026-10-18
- 状态：已实现（与本设计同一个 PR）。§8 列出实现时采用的默认决定，review 时可推翻
- 范围：`tests/` 下所有连接 PostgreSQL 的 fixture、`.github/workflows/ci.yml` 的测试 job、
  `Makefile` 的 `test-db` 目标。**不改任何生产代码。**
- 相关记录：#1466（CI 中真正执行数据库测试）、#1472（operational store 缺列）、
  #1545（#1466 暴露的配额注释错误）、#1546（共享 HTTP 客户端的测试污染）

## 1. 背景

#1466 之前，CI 里约 300 个数据库测试一直被跳过：PostgreSQL service 的端口是随机映射的，
CI 只导出了 `DB_PORT`，而多数 fixture 读的是 `TEST_DB_PORT`，没读到就回退到 5432，
连接失败后执行 `pytest.skip(...)`。skip 不算失败，所以 CI 长期是绿的。
#1472 修复的缺列 bug、#1545 修正的错误断言，都是这些测试从未执行而遗留下来的。

#1466 在 CI 这一侧把问题堵上了：导出完整的 `TEST_DB_*`，新增串行的 `db` 分片，
在测试前建好表结构并为每个 xdist worker 复制一份库，最后用
`ops/ci/check_pytest_skips.sh` grep 日志，发现与数据库相关的 SKIPPED 就判失败。

但根因没有变：**每个测试文件各自决定连哪个库、连不上怎么办。** #1466 只是让 CI
恰好提供了所有文件各自需要的变量，并在事后用文字匹配兜底。

## 2. 现状（基于 `dev` @ `c870fed8`）

### 2.1 连接配置有三套来源，散落在约 29 个文件里

完整清单见附录 A。归纳如下：

| 来源 | 文件数 | 读法 | 没设置时 |
|---|---|---|---|
| `TEST_DB_HOST/PORT/NAME/USER/PASSWORD` | 16 | 逐个 `os.getenv`，多数自己拼 xdist 后缀 | 回退到 `localhost:5432` |
| `TEST_PG_DSN` | 12 | 一个 DSN | 直接 skip："TEST_PG_DSN is not set" |
| `PG_TEST_DSN` | 1（`unit/storage/test_login_events.py`） | 一个 DSN | 直接 skip |
| `DB_HOST/PORT/NAME/...` | 4（`TEST_PG_DSN` 的后备） | 应用自己的变量 | 回退到 `localhost:5432` |

此外：

- xdist worker 后缀（`<base>_gwN`）在 15 个文件里各自拼接。#1466 修过的
  `_gw0_gw0` 重复后缀，就是 `tests/servers/conftest.py` 把带后缀的库名写回
  `TEST_DB_NAME` 造成的。`test_browser_session_races`、`test_phone_auth_codes`、
  `test_revoke_user_sessions` 不加后缀，所有 worker 连同一个基础库，靠每个测试自建
  schema 隔离。这是一种合理做法，但它是第三种约定。
- 哪个 fixture 负责**创建** worker 库也不统一：`tests/servers/conftest.py` 和少数
  integration fixture 会 `CREATE DATABASE`，其余默认它已经存在（见 §2.5）。
- "库名必须包含 `_test_`"的安全检查，有的文件调用 `tests/fixtures/auth_helpers.py`
  的 `assert_test_db_name`，有的自己写一份，DSN 一类的文件没有这项检查。
- 新代码还在继续发明变量：未合并的 #1517 新增了 `CACHE_TELEMETRY_TEST_DB_PORT` /
  `CACHE_TELEMETRY_TEST_DB_NAME`，并且注释说明要"优先 `DB_*`，因为 runner 上的
  `TEST_PG_DSN` 可能指向别的端口"。

### 2.2 连不上时的处理：约 30 处 skip，8 种措辞

`pytest.skip` 的原因包括 `PostgreSQL not available: …`（14 处）、
`TEST_PG_DSN is not set; …`（8 处）、`PostgreSQL test database not available: …`、
`PostgreSQL auth test database is not available.`、`PG_TEST_DSN not set; …`、
`This test requires PostgreSQL (auth_db_logger)`、
`Database not available (start PostgreSQL or check DB config)` 等。

CI 的 grep 兜底依赖这些措辞，而且已经有漏洞：普通分片用的正则是
`SKIPPED.*(PostgreSQL .*not available|…)`，匹配不到 `tests/servers/conftest.py`
里 `require_db` 的 `Database not available (start PostgreSQL …)`，因为在这句话里
"PostgreSQL" 出现在 "not available" 之后。`tests/servers/test_admin.py`、
`tests/servers/test_concurrency_endpoint.py` 用到了 `require_db` / `auth_app`，
却没有 `dbtest` 标记，会在普通分片中运行，正好落在这个漏洞上。

### 2.3 表结构准备只在 CI 里做

`ci.yml` 的 "Prepare PostgreSQL test databases" 步骤内嵌了一段 Python：依次执行
`DatabaseLogger.initialize()`、`PostgresOperationalStore.initialize()`、
`ResponseStore.initialize()`，再用这个库作模板 `CREATE DATABASE … TEMPLATE …`
复制出 `_gw0 … _gwN`。

本地没有对应的步骤。`tests/servers/conftest_auth.py::_init_pg_backend` 只执行
`DatabaseLogger.initialize()`，而 `storage/database.py` 和
`storage/postgres_operational.py` 各有一份 `users` / `api_keys` 建表语句（#1472 的
review 中已指出），`signup_reason` 等 operational 列只由后者的 `initialize()`
（含补列迁移）添加。在一个只执行过 `DatabaseLogger.initialize()` 的库上，这些列就不存在。

串行跑整套时这一点被掩盖了：默认收集顺序下 `tests/integration/` 排在
`tests/servers/` 之前，前者的 fixture 已经在同一个库上执行过
`PostgresOperationalStore.initialize()`。这依赖测试顺序，并不是有意设计的保证。
实测：在空库上单独串行运行 `tests/servers/test_user_routes.py -m dbtest`，
结果是 3 passed、33 errors，全部是
`column "signup_reason" of relation "users" does not exist`。

### 2.4 实测：照 `make test-db` 的提示在本地跑

`make test-db` 的帮助文字是 "set TEST_DB_* env vars"。在一个全新的 PostgreSQL 16
（`127.0.0.1:55450`）上，只设置 `TEST_DB_*`，串行执行
`pytest -m "dbtest and not external"`：

```text
252 passed, 92 skipped
```

92 个 skip 中，3 个是缺少外部 API key（预期内），其余 89 个都是数据库相关：

| 原因 | 数量 | 文件 |
|---|---|---|
| `TEST_PG_DSN is not set` / `PG_TEST_DSN not set` | 51 | `test_provider_stats_rollup`、`test_database_integration`、`test_app_config_store_postgres`、`test_login_events` 等 9 个 |
| `password authentication failed for user "postgres"` | 35 | `test_admin_request_perf_breakdown_postgres`、`test_admin_request_offloads_postgres`、`test_agent_grant_usage_window_postgres`、`test_admin_client_disconnect_postgres` |

第二类值得单独说明：这 4 个文件在没有 `TEST_PG_DSN` 时读 `DB_*`，没设置就回退到
`localhost:5432`。这台机器的 5432 上跑着另一个 PostgreSQL 实例（不属于本次测试），
这些测试连了过去，只是因为密码不对才被拒绝。默认库名 `hybridinference_test_db`
本身就能通过 `_test_` 检查，所以如果密码碰巧一致，它们会在那个实例上建 schema、写数据。
这里只说明存在这条路径，不推断那个实例上有什么数据。

### 2.5 实测：变量设齐以后，串行能过，并行不能

同一个实例、每次都换一个空库，把 CI 设置的变量全部设上（`TEST_DB_*`、`DB_*`、
`TEST_PG_DSN`、`PG_TEST_DSN`，以及 `USER_AUTH_ENABLED`、`API_KEY_SECRET`、
`ERASURE_FENCE_SECRET`），**不执行** CI 的表结构准备步骤：

| 运行方式 | 结果 |
|---|---|
| 串行（与 CI 的 `test (db)` 相同） | 341 passed、3 skipped，与 CI 完全一致 |
| `-n 8 --dist=loadfile` | 204 passed、17 failed、74 errors、49 skipped |

并行时的失败只有两类：

| 现象 | 数量 | 原因 |
|---|---|---|
| `UndefinedColumnError`：`signup_reason`（76）、`max_concurrent_requests`（8）、`pending_email`（1） | 85 个 failed / error，集中在 `tests/servers/` | `tests/servers/conftest.py` 自己 `CREATE DATABASE` 出 `_gwN`，但 `_init_pg_backend` 只做 `DatabaseLogger.initialize()`（§2.3），operational 的列没有补上 |
| skip：`database "hybridinference_test_db_gwN" does not exist` | 46（`test_erasure_fence_races` 34、`test_identity_auth_codes` 6、`test_first_run_setup_postgres` 6） | 这些 fixture 默认 worker 库已存在，自己不创建 |

所以 CI 能通过，靠的是两件只在 CI 里成立的事：`db` 分片串行运行，以及内嵌 Python
提前准备好了表结构和 worker 库。上次本地排查时把这些失败归因为"测试之间互相弄乱表结构"，
实测并非如此：自建精简表的测试都在独立 schema 里，删列的
`test_reset_password_cli_postgres` 在 teardown 中会重新 `initialize()`。

## 3. 目标与非目标

**目标**

1. 测试连哪个库只有**一个入口**。新写一个数据库测试，不需要知道任何环境变量名。
2. 在要求有数据库的环境里（CI），连不上数据库时**失败**，而不是跳过。这个结论由
   fixture 本身决定，不依赖日志措辞。
3. 不再回退去连一个没人指定过的 `localhost:5432`。
4. 本地用一条命令，在一个空库上准备好表结构并跑完整套数据库测试，结果与 CI 一致。
5. 有一个结构性的守卫，防止新代码绕开入口。

**非目标**

- 合并 `storage/database.py` 和 `storage/postgres_operational.py` 的两份建表语句。
  这是生产代码的问题，另立设计。
- 改变数据库测试的分片方式或并行度（见 §8 Q2）。
- 修改各测试自身的断言或业务逻辑，迁移只替换"如何拿到连接"。

## 4. 设计

### 4.1 唯一入口：`tests/fixtures/postgres.py`

文件名不能以 `test_` 开头，否则会被 pytest 当作测试模块收集；同理，入口里的函数和类名
也不以 `test` 开头（`target()` / `DatabaseTarget`），否则被测试文件 import 后会被当成测试。

```python
@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    user: str
    password: str
    base_name: str   # TEST_DB_NAME 原值，永不修改
    name: str        # 本 worker 实际使用的库：base_name 或 base_name_gwN

    def connect_kwargs(self, database=None) -> dict[str, Any]: ...  # 给 asyncpg / DatabaseLogger
    def dsn(self, database=None) -> str: ...
    def app_env(self) -> dict[str, str]: ...    # DB_HOST/DB_PORT/DB_NAME/... 给被测应用


def target(*, worker=None) -> DatabaseTarget: ...
def configuration_problem() -> str | None: ...           # 不连接，只检查环境
def database_required() -> bool: ...                     # TEST_DB_REQUIRED
def database_unavailable(reason: str) -> NoReturn: ...   # skip 或 fail 的唯一出口

async def connect(*, database=None, timeout=5, **kwargs) -> asyncpg.Connection: ...
async def create_pool(*, database=None, **kwargs) -> asyncpg.Pool: ...
async def connect_maintenance() -> asyncpg.Connection: ...   # postgres 库，用于建库
async def worker_connect_kwargs() -> dict[str, Any]: ...      # 交给自己建池的网关代码
def worker_dsn() -> str: ...                                  # 同步 fixture 用，先探测可达
async def ensure_database(name, *, template=None) -> bool: ...
async def prepare_schema(pool) -> None: ...
async def prepare_databases(workers: int = 0) -> list[str]: ...
```

要点：

- **输入只认 `TEST_DB_HOST/PORT/USER/PASSWORD/NAME`。** 被测应用读的 `DB_*`
  由 `app_env()` 生成，并由 fixture 通过 `monkeypatch.setenv` 设置。方向只有一个：
  `TEST_DB_*` → `DB_*`，不再反过来把 `DB_*` 当作测试库的来源。
- **worker 后缀只在这里拼。** `base_name` 来自 `TEST_DB_NAME`，任何地方都不写回；
  `name` 由 `PYTEST_XDIST_WORKER` 推导。
- **安全检查只在这里做，而且做两次：** 解析配置时检查 `name` 包含 `_test_`；
  `connect` / `create_pool` 成功后再用 `SELECT current_database()` 检查一次
  （复用现有的 `assert_test_db_name` / `assert_test_db_from_pool`，挪进本模块）。
- **只有"连不上"才走 `database_unavailable(reason)`。** `connecting(what)` 只把
  `unavailable_errors()` 列出的异常（连接被拒、超时、认证失败、库不存在、server 正在启动或
  连接数已满）转成 skip / fail。SQL 错误、参数错误、`init=` 回调里的错误是代码 bug，原样抛出，
  非 required 模式下也不会被记成 skip。
- **worker 库由入口按需创建并准备表结构。** `connect()` / `create_pool()` / `worker_dsn()` /
  `worker_connect_kwargs()` 第一次连接本 worker 的库（串行时即基础库）时，若它不存在就创建，
  并执行一次 `prepare_schema`（每个进程每个库一次；在准备过的库上只读目录）。
  §2.5 中 46 个 "does not exist" 的 skip 来自"谁负责建库"的不一致；某个文件自己建了一张
  精简版 `api_logs`、导致后面的文件缺列，来自"谁负责建表"的不一致。两者都收归入口后，
  任何一个数据库测试单独运行或先运行，看到的都是完整的表结构，不再依赖顺序。
- 把 DSN 或连接参数交给网关代码（`DatabaseLogger({"dsn": …})`）的 fixture，用
  `worker_dsn()` / `worker_connect_kwargs()`：前者会先连一次、检查库名，连不上就在这里
  skip 或 fail，而不是让网关代码抛出一个看起来像 bug 的异常。
- 需要独立 schema 或独立库的测试（例如 `test_users_schema_lock_postgres`、
  `test_admin_request_*` 的 `CREATE SCHEMA … SET search_path`）照旧自己管理
  schema，只把"连到哪个库"交给入口。`create_pool(**overrides)` 允许传
  `server_settings` 等 asyncpg 参数，但不允许覆盖 host、port 和库名。

### 4.2 环境变量契约

| 变量 | 含义 | 未设置时 |
|---|---|---|
| `TEST_DB_HOST`、`TEST_DB_PORT` | 测试 PostgreSQL 地址 | 非 required：回退到 `localhost:5432`（保持本地习惯）；required：会话启动即失败 |
| `TEST_DB_USER`、`TEST_DB_PASSWORD` | 凭据 | `postgres` / `postgres` |
| `TEST_DB_NAME` | 基础库名，必须含 `_test_` | `hybridinference_test_db` |
| `TEST_DB_REQUIRED` | `1` 表示这个环境必须有数据库 | 视为 `0` |
| `TEST_PG_DSN`、`PG_TEST_DSN` | **废弃**，不再读取 | 若检测到已设置而 `TEST_DB_HOST` 未设置，会话启动时报错，提示改用 `TEST_DB_*` |

对废弃变量报错而不是静默忽略：一个本地只设了 `TEST_PG_DSN` 的开发者，升级后不应该
不知不觉地改连 `localhost:5432`。

非 required 时仍保留 `localhost:5432` 这个回退，是为了不打断本地"起一个默认端口的
PostgreSQL 就能跑"的习惯。真正危险的回退（`DB_*` → 5432）随 `DB_*` 输入一起移除。
是否连非 required 的回退也一并去掉，见 §8 Q3。

### 4.3 失败策略：`TEST_DB_REQUIRED`

- `database_unavailable(reason)`：required 时 `pytest.fail(reason)`，否则
  `pytest.skip(reason)`。全仓库只剩这一处数据库相关的 skip。
- `tests/conftest.py` 增加 `pytest_sessionstart` 预检（只在主进程执行，xdist worker
  跳过）：任何模式下先跑 `configuration_problem()`（废弃变量、端口、库名）；required 时
  再检查 `TEST_DB_HOST` / `TEST_DB_PORT` 已设置并试连一次 server 的 `postgres` 维护库
  （测试库本身由入口在首次使用时创建）。失败就用
  `pytest.exit(…, returncode=ExitCode.USAGE_ERROR)` 终止整个会话，让问题在一行里说清楚，
  而不是变成几百个相同的失败。
- CI 的所有测试 job（`test (1)`…`test (4)`、`test (db)`）都设置
  `TEST_DB_REQUIRED=1`。它们本来就都启动了 postgres service。
- `make test-db` 设置 `TEST_DB_REQUIRED=1`：既然显式要求跑数据库测试，就不应该静默跳过。
  `make test` 不设置，也不受影响，因为它本来就排除 `dbtest`。

有了这一层，`ops/ci/check_pytest_skips.sh`、`tests/unit/ops/test_ci_skip_guard.py` 和
CI 中所有 grep 日志的步骤一并删除：它们判断的是 skip 的措辞，而这里 skip 根本不会发生。
`test (1)` 中单独串行运行 `test_erasure_fence_races.py` 的步骤也删除——`test (db)` 已经
串行运行包括它在内的全部 `dbtest`，那一步只是重复执行，外加一套已不需要的计数检查。

### 4.4 表结构准备只有一份

- `prepare_schema(pool)`：依次执行 `DatabaseLogger.initialize()`、
  `PostgresOperationalStore.initialize()`、`ResponseStore.initialize()`，与 CI
  现有步骤完全一致。
- `prepare_databases(workers)`：在 `base_name` 上执行 `prepare_schema`，再用它作模板
  创建尚不存在的 `_gw0 … _gw{workers-1}`；已存在的 worker 库（例如上次本地运行留下的）
  就地再执行一次 `prepare_schema`，补齐新列。
- 模块提供命令行入口：`python -m tests.fixtures.postgres prepare --workers N`。
  - `ci.yml` 中那段内嵌 Python 换成这条命令。
  - `make test-db` 在 pytest 之前先执行它。
  - 它不经过 pytest，所以自己把 pytest 的 `pythonpath`（`apps/backend`、`.`）放到
    `sys.path` 最前面，并在 `serving` 不是来自本 checkout 时拒绝执行。否则在 worktree 里
    复用另一个 checkout 的虚拟环境时，会用那个 checkout 的建表代码准备数据库。
  - 有了按需准备（§4.1），它对正确性不再是必需的；保留它是因为用模板复制 worker 库比逐个
    迁移快，并且让 CI 在测试开始前就暴露建表错误。
- `tests/servers/conftest_auth.py::_init_pg_backend` 通过 `worker_connect_kwargs()`
  拿到已准备好的库，不再只依赖 `DatabaseLogger.initialize()`。

### 4.5 结构性守卫

沿用 `tests/unit/config/test_no_raw_env_reads.py` 的 AST 扫描写法，新增
`tests/unit/test_database_entrypoint.py`，扫描 `tests/` 和 `distributions/*/tests/`：

- 在 `tests/fixtures/postgres.py` 以外读取 `TEST_DB_*`、`TEST_PG_DSN`、`PG_TEST_DSN`，失败。
- 在 `tests/fixtures/postgres.py` 以外直接调用 `asyncpg.connect` / `asyncpg.create_pool`
  （或 `from asyncpg import connect / create_pool`），失败。
- 允许名单 `ALLOWED` 只有入口本身。用同一套扫描去扫 `dev` @ `c870fed8` 的 `tests/`，
  会在 29 个文件里报出 152 处，正好是附录 A 的范围。

这个守卫取代 grep 日志：它不关心 skip 的措辞，只保证没有第二条连接路径。

### 4.6 测试归类（可选，见 §8 Q1）

用到 `require_db`、`auth_app` 等真实数据库 fixture、却没有 `dbtest` 标记的测试
（目前确认的有 `tests/servers/test_admin.py`、`tests/servers/test_concurrency_endpoint.py`），
可以在 `tests/conftest.py` 的 `pytest_collection_modifyitems` 中按 fixture 名自动加上
`dbtest`。这样 `make test` 在没有数据库的机器上会排除它们，CI 会把它们放进 `db` 分片。

## 5. 迁移示例

迁移前（`tests/integration/test_provider_stats_rollup.py` 一类）：

```python
@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.getenv("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN is not set; skipping database integration tests")
    return dsn
```

迁移后：

```python
from tests.fixtures import postgres

@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return postgres.worker_dsn()
```

迁移前（`TEST_DB_*` 一类，约 15 行重复代码）：解析 worker 后缀、检查 `_test_`、
`asyncpg.create_pool(...)`、`except Exception: pytest.skip(...)`。

迁移后：

```python
from tests.fixtures import postgres

pool = await postgres.create_pool(min_size=1, max_size=5)
```

## 6. 实施

原计划分三期（P1 入口与迁移、P2 表结构准备、P3 等 #1463 合并后迁移重叠文件并删除 grep
兜底）。最终按一个 PR 一次完成：

- 新增 `tests/fixtures/postgres.py`、`tests/unit/test_postgres_fixture.py`（入口本身）、
  `tests/unit/test_database_entrypoint.py`（§4.5 守卫）。
- 迁移附录 A 的全部文件，**包括** #1463 也修改的 4 个。这 4 个文件里只改了连接 fixture
  的那几行，测试函数本身没有动，以减小 #1463 合并 `dev` 时的冲突面。
- `tests/fixtures/auth_helpers.py` 的 `assert_test_db_name` 等改为从入口重新导出，原有
  import 不受影响；`build_auth_test_env_defaults()` 的 `DB_NAME` 改为本 worker 的库。
- `tests/servers/conftest_auth.py::_init_pg_backend` 改用 `worker_connect_kwargs()`，
  库在 `DatabaseLogger.initialize()` 之前已由入口准备好。
- `ci.yml`：准备步骤改为 `python -m tests.fixtures.postgres prepare`；所有测试步骤设置
  `TEST_DB_REQUIRED=1`；删除 `TEST_PG_DSN` / `PG_TEST_DSN`、grep 兜底和重复的
  erasure-fence 步骤（§4.3）。普通分片仍导出 `DB_*`，因为被测网关本身读它们。
- `make test-db` 先 `prepare` 再测试，二者都设置 `TEST_DB_REQUIRED=1`。
- `docs/developer/contributing.md` 及其中文翻译同步更新。

初版曾留下一个问题：`test_growth_series_postgres.py` 在找不到 `api_logs` 时自己建一张精简版
的表，在空库上先运行它，后面的 `test_provider_stats_rollup.py` 会报
`column "session_id" does not exist`。review 后改为由入口按需准备表结构（§4.1），这张精简表
也随之删除。

review 还指出的另外两处也已修正：连接异常的分类（§4.1，原先 `except Exception` 会把
`init=` 回调中的 SQL 错误记成 skip）；命令行入口的导入路径（§4.4）。

## 7. 验证

以下都在本机、全新的 PostgreSQL 16 容器上执行（`fsync=off`），代码为本 PR 的最终版本；
"CI 环境"指与 `ci.yml` 中对应步骤相同的变量。

| 场景 | 迁移前（`dev`） | 迁移后 |
|---|---|---|
| 只设置 `TEST_DB_*`，串行运行 `-m "dbtest and not external"`（§2.4） | 252 passed，92 skipped（89 个与数据库有关） | **341 passed，3 skipped**（3 个都是外部 API key） |
| CI `test (db)` 环境，串行 | 341 passed，3 skipped | 341 passed，3 skipped |
| 同上，`-n 8`，库已 `prepare` | —— | 341 passed，3 skipped |
| 同上，`-n 8`，**空库、不 prepare**（§2.5） | 17 failed，74 errors，49 skipped | **341 passed，3 skipped** |
| 空 server 上执行 `make test-db` | —— | 341 passed，3 skipped（`prepare` 自动建好基础库） |
| CI 普通分片环境，`-m "not external and not dbtest" -n 8` | —— | 7781 passed，7 skipped（均与数据库无关） |
| 不设置任何 `TEST_DB_*`，`make test` | 7740 passed，15 skipped | 7773 passed，15 skipped；skip 原因与迁移前逐条相同，多出的是新增单元测试 |
| `TEST_DB_REQUIRED=1`，端口无人监听 | —— | 会话立即退出（exit 4），一行说明地址与原因 |
| `TEST_DB_REQUIRED=1`，未设置 `TEST_DB_HOST` | —— | 会话立即退出，说明拒绝回退到 `localhost:5432` |
| 只设置 `TEST_PG_DSN` | —— | 会话立即退出，提示改用 `TEST_DB_*` |
| 未设置 `TEST_DB_REQUIRED`，端口无人监听，`-m dbtest -n 8` | —— | 5 passed，339 skipped，没有 error |
| 守卫扫描 `tests/` | 29 个文件 152 处 | 0 |
| review 后：空 server，CI `test (db)` 环境，`-n 8`，不 prepare | —— | 343 passed，3 skipped（多出的 2 个是新增的入口数据库测试） |
| review 后：CLI `prepare --workers 8` 后串行 | —— | 343 passed，3 skipped |
| review 后：空库上依次运行 growth、provider rollup、`test_user_routes.py` | 4 passed，10 errors（`session_id` 缺列） | 58 passed |
| review 后：在 worktree 中用主 checkout 的虚拟环境执行 `prepare` | 建表代码来自主 checkout | 建表代码来自本 worktree |
| review 后：healthy server 上 `create_pool(init=…)` 执行错误 SQL | 记为 skip（非 required） | 抛出 `UndefinedColumnError` |

验证过程中发现并修复了三个问题：

1. `tests/servers/conftest.py` 的会话级 autouse fixture 在创建 worker 库失败时会被
   `pytest.skip` 打断（它是 `BaseException`，`suppress(Exception)` 拦不住），导致没有数据库时
   **全部** server 测试被跳过（`make test` 一度变成 6141 passed、1645 skipped）。已修复，
   并加了回归测试。
2. `test_postgres_hard_delete_audit_rows.py` 与 `test_erasure_fence_races.py` 用不同的
   erasure-fence 密钥写同一个库，谁先运行谁的指纹生效，另一个在 `-n` 下报
   `ErasureFenceUnavailable`。迁移前的并行实测里同样出现（这 2 个 error 包含在 §2.5 的
   74 个里）。统一为同一个测试密钥。
3. `test_verify_api_key_against_real_database` 只从 CI 的 job 环境取 `USER_AUTH_ENABLED` /
   `API_KEY_SECRET`。迁移前它在本地因为没有 `TEST_PG_DSN` 而被跳过，所以没人发现；现在它
   在缺少这两个变量时自行补上测试值，CI 设置的值保持不变。

## 8. 实现时采用的决定（review 时可推翻）

- **Q1 测试归类：不做。** 使用真实数据库却没有 `dbtest` 标记的测试（`test_admin.py`、
  `test_concurrency_endpoint.py`）仍在普通分片中运行。普通分片现在也设置了
  `TEST_DB_REQUIRED=1`，它们连不上时会失败，§2.2 的漏洞因此已经堵上；是否把它们移进
  `db` 分片是分类问题，另议。
- **Q2 `db` 分片并行化：不做。** 本 PR 之后已经可行（§7 中 8 进程并行通过），但改变
  CI 拓扑应单独评估耗时与 runner 预算。
- **Q3 非 required 的 `localhost:5432` 回退：保留。** 只在没有设置 `TEST_DB_REQUIRED` 时
  生效；CI 与 `make test-db` 都不会走到这条路径。
- **Q4 废弃变量：报错。** 只设置 `TEST_PG_DSN` / `PG_TEST_DSN` 而不设置 `TEST_DB_HOST` 时，
  会话启动即退出并说明原因；两者都设置时忽略前者。
- **Q5 #1517：** 尚未合并。它新增的 `CACHE_TELEMETRY_TEST_DB_*` 会被 §4.5 的守卫拦下
  （`asyncpg.connect` 与 `TEST_PG_DSN` 读取），合并前需要改用入口。

## 附录 A：读取测试库配置的文件

"TDB" 表示 `TEST_DB_HOST/PORT`，"DSN" 表示 `TEST_PG_DSN`，"PGT" 表示 `PG_TEST_DSN`，
"DB_" 表示 `DB_HOST/PORT`；数字是出现次数。"后缀"表示是否处理 xdist worker 后缀。
`tests/unit/config/` 等处读 `DB_*` 是在测试应用自身的配置解析，不是连接测试库，不在此列。

| 文件 | TDB | DSN | PGT | DB_ | 后缀 | 与 #1463 重叠 |
|---|---|---|---|---|---|---|
| `fixtures/auth_helpers.py` | 2 | | | 2 | | |
| `integration/storage/test_agent_grant_usage_window_postgres.py` | | 2 | | 2 | ✓ | |
| `integration/storage/test_browser_session_races.py` | 2 | | | | ✗ | ✓ |
| `integration/storage/test_erasure_fence_races.py` | 2 | | | | ✓ | ✓ |
| `integration/storage/test_first_run_setup_postgres.py` | 2 | | | | ✓ | |
| `integration/storage/test_identity_auth_codes.py` | 2 | | | | ✓ | |
| `integration/storage/test_phone_auth_codes.py` | 2 | | | | ✗ | ✓ |
| `integration/storage/test_postgres_hard_delete_audit_rows.py` | 2 | | | | ✓ | |
| `integration/storage/test_postgres_key_owner_audit.py` | 2 | | | | ✓ | |
| `integration/storage/test_postgres_provider_keys.py` | 2 | | | | ✓ | |
| `integration/storage/test_postgres_role_quota.py` | 4 | | | | ✓ | |
| `integration/storage/test_reset_password_cli_postgres.py` | 2 | | | 4 | ✓ | |
| `integration/storage/test_revoke_user_sessions.py` | 2 | | | | ✗ | ✓ |
| `integration/storage/test_users_at_daily_quota_postgres.py` | 4 | | | | ✓ | |
| `integration/storage/test_users_schema_lock_postgres.py` | 2 | | | | ✓ | |
| `integration/test_admin_client_disconnect_postgres.py` | | 2 | | 2 | ✓ | |
| `integration/test_admin_request_offloads_postgres.py` | | 2 | | 2 | ✓ | |
| `integration/test_admin_request_perf_breakdown_postgres.py` | | 3 | | 2 | ✓ | |
| `integration/test_app_config_store_postgres.py` | | 2 | | | | |
| `integration/test_database_integration.py` | | 2 | | | | |
| `integration/test_geo_demand_rollup_postgres.py` | | 2 | | | | |
| `integration/test_growth_series_postgres.py` | | 2 | | | | |
| `integration/test_pricing_snapshot_postgres.py` | | 2 | | | | |
| `integration/test_provider_stats_rollup.py` | | 2 | | | | |
| `servers/conftest.py` | 6 | | | 4 | ✓ | |
| `servers/conftest_auth.py` | 2 | | | | | |
| `servers/test_admin_provider_stats.py` | | 3 | | | | |
| `servers/test_admin_token_usage.py` | | 3 | | | | |
| `unit/storage/test_login_events.py` | | | 4 | | | |

另有 `unit/ops/test_ci_skip_guard.py` 与 `unit/test_auth_test_helpers.py`，它们测试的是
守卫和 helper 本身，随 P1 / P3 一起调整。
