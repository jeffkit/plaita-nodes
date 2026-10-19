# plaita-nodes 沙箱执行抽象（Sandbox Drivers）设计方案

> 状态：**v2.1（四路交叉评审 + 复核通过：10/10 Blockers CLOSED，PASS_WITH_NITS 已修）**
> 目标仓：plaita-nodes
> 关联：ADR-2026-08-27（编排单轨 plaita + 执行层 agentproc）、大仓 `docs/ARCHITECTURE.md` §5.1
> 日期：2026-10-01
> 评审记录见 §15；本版已裁决 v1 全部 10 条 Blockers，裁决以**加粗「裁决」**标注。

---

## 0. 背景与问题（v2 修正）

coding 多 agent 协同场景下，agentrun 节点调用的 Agent CLI（recursive / claude-code / …）
需要在隔离沙箱里落地执行（本地 Docker / SSH 可达 VM / 云沙箱 API 如 E2B），否则 agent 的
工具调用（bash / edit / 测试）直接跑在宿主文件系统上。

现状（`plaita-nodes/src/plaita_nodes/agent_run.py`）：

- `AgentRunNode.repo` → `RECURSIVE_WORKSPACE`（宿主路径，直跑）；
- `RECURSIVE_BIN` 可替换二进制（agent_run.py:41-42）；
- 执行走 agentproc Python SDK in-process executor：`build_args(message, session_id, env) -> argv`，
  runner 以 `subprocess.Popen(argv, start_new_session=True, env=env)` 拉起
  （`agentproc/sdk/python/src/agentproc/runner.py:567-614`）。

两条底线：

1. **脑手分离**：plaita 控制面（FlowWorker / ExecutionStorage / EventBus）永远留宿主，
   沙箱只承载执行面。
2. **断点续跑红利不丢**。**如实陈述（v2）**：flow 级 checkpoint 在宿主 Redis，与 agent
   在哪跑无关，这条不受沙箱化影响；agent 级会话续跑在 plaita-nodes **现状为 0**——
   agent_run 从不向 RunOptions 传 session_id、recursive-direct 的 build_args 忽略 session_id
   （agent_run.py:40-52, 281-286），因此沙箱化**不会丢失**存量不存在的能力。未来接通
   session 续跑的前提是 CLI 会话目录进入数据层（§6 挂起策略、§10 断言 9），per-driver 声明。

## 1. 设计原则（v2 增补 2 条）

1. **边界 = 共享可变状态的边界**：同一条接力链共享一个 workspace；链间、并行 fan-out、
   验证与写作之间分沙箱，靠 git 交换。
2. **计算层 / 数据层分离**：容器/VM/沙箱实例是易逝计算层；工作副本（volume / git remote）
   是要活过崩溃的数据层。**ssh driver 计算层与数据层同盘，数据层承诺降级（§5、§6）**。
3. **数据交换统一走 git**（无文件传输 API）：provision 时 clone 进去，出活时 push 出来。
   本地 Docker 亦不 bind-mount 宿主目录。边界声明：clone 进来的 repo 本身是攻击载荷载体
   （安装脚本 / hooks / prompt injection in 源码），隔离它靠 §7 的 egress、run 画像与
   token 最小化，不靠「不 bind-mount」。
4. **确定性命名**：handle 由 `(execution_id, ws_key)` 派生，不靠 context 存活；
   resume 后 `$LOOP-INDEX`/`$INPUT.index` 重算出相同 key 序列 → 幂等 attach 免费续上。
   start 任务重投（新 execution_id）不在该红利覆盖面内，由 status-aware reaper 闭环（§6）。
5. **节点 API 最小面**：agentrun 只加 `workspace` 字段（选哪个）；**定义权收归 infra
   注册表**（§4，v1 曾放 flow YAML，评审否决）；`sandbox.py` 管怎么活。
6. **执行权模型（v2 新增，回应评审 A-B1/B-B1/B-B2）**：
   - **本地 argv driver（docker / ssh）**：agentproc runner 仍是唯一执行者，driver 不拥有
     agent 进程的 exec——driver 产出**宿主可执行包裹 argv**，runner 照常 Popen；
   - **API driver（e2b 等）**：argv 类型在此无消费者（runner 硬编码本地 Popen），plaita-nodes
     显式**分叉**自建等价执行循环（§3.2），复用 agentproc 导出纯函数，conformance 加
     wire 等价断言；
   - **沙箱内进程的最终击杀权在 driver**（§6.4），不依赖宿主客户端存活。
7. **不改 agentproc wire protocol**（stdin turn / stdout NDJSON 一个字节不动）、不改 plaita
   core。措辞边界（v2）：若将来 executor 接口需扩展（如「委托外部执行器」），走 agentproc
   spec doc revision + SDK bump 正规流程立项，不在本方案内单方面宣布。

## 2. 总体架构（v2：显式分叉）

```
宿主机
├─ 控制面（脑，零改动）
│   plaita FlowWorker ── checkpoint(宿主 Redis) / EventBus / HITL
│   └─ agentrun 节点
│        ├─ 无 workspace 字段 → agentproc runner.run（今天的行为，逐字节一致）
│        └─ 有 workspace 字段 → sandbox.py 解析 .plaita/sandboxes.json 注册表
│             ├─ driver=docker/ssh（本地 argv 型）
│             │    driver.ensure → 包裹 argv ──▶ agentproc runner.run（Popen）
│             │         `docker run --rm -i … <image> timeout <T> <agent argv…>`
│             │         `ssh <host> timeout <T> <agent argv…>`
│             └─ driver=e2b（API 型）→ 分叉：plaita-nodes 等价执行循环
│                  （复用 agentproc 纯函数 classify_line / parse_json_line /
│                   is_valid_session_id；conformance 断言 wire 等价，§10.5）
├─ 数据层：named volume / git remote（provision=clone，出活=push）
└─ 安全机制：env 白名单（credentials 引用）+ canary 脱敏 + token TTL + egress 策略
```

对 flow 引擎而言 agent 仍是一次同步调用：prompt 进、结果 JSON 出
（`{"text","cli","model","session_id","usage","observations","workspace"}`）。

## 3. 接口契约（plaita-nodes `sandbox.py`）

### 3.1 Driver 协议（v2 收窄：本地 argv 型 driver 不拥有 exec）

```python
class SandboxDriver(Protocol):
    def ensure(self, spec: ResolvedSpec) -> WorkspaceHandle:
        """幂等 attach/create；provision（clone）耗时算在这里。
        强校验：image/template 未 pin（digest 或注册表白名单）→ 拒绝。"""

    def wrap_argv(self, handle, agent_argv: list[str],
                  timeout_secs: int, envfile: str) -> list[str]:
        """仅本地 argv 型 driver 实现：产出宿主可执行包裹 argv。
        沙箱内以 `timeout <T>` 自持墙钟（第一击杀权，不依赖宿主客户端存活）。"""

    def enforce(self, handle, *, force: bool = False) -> None:
        """最终击杀权：超时/异常后强制清理沙箱内进程树
        （docker rm -f → 内核对 PID namespace 全杀；ssh 按远端标记 pkill）。"""

    def git(self, handle, args: list[str]) -> CompletedResult:
        """数据面专用：clone / status --porcelain / add / commit / push。
        v1 不开放通用 exec（数据交换只经 git）。"""

    def release(self, handle, *, keep_data: bool = True) -> None:
        """默认只毁计算层；数据层另有 TTL/status-aware reaper 兜底（§6.3）。"""
```

- `ExecResult` 由 runner 路径返回（本地 argv 型），或 API 分叉循环返回；**stdout/stderr
  已过 canary 脱敏层（§7.1）**。
- API 型 driver 另有 `exec_process(handle, argv, env, stdin, timeout, on_line)`（平台
  process API），仅供 §3.2 分叉循环调用，不进通用契约。

### 3.2 API driver 分叉循环（e2b 等）

plaita-nodes 内实现与 agentproc runner **可观察等价**的执行循环：组装 stdin turn、
消费 stdout、复用 agentproc 导出纯函数（`classify_line` / `parse_json_line` /
`is_valid_session_id`，均在 runner 模块 `__all__`），session_id 取 first-non-empty、
超时/退出码语义对齐 runner。conformance 断言：同一 fake agent 输入下，两条路径产出
逐字段相同的 RunResult（§10.5）。后续正解：向 agentproc 立项「executor 委托外部执行器」
扩展（spec doc revision + SDK bump），归一三类 driver，届时本分叉收编。

### 3.3 注册表

沿用仓内先例（`DECISION_PROVIDERS` / `NOTIFY_BACKENDS`）：

```python
SANDBOX_DRIVERS: dict[str, SandboxDriver] = {}
# entry_points 组 "plaita_nodes.sandbox_drivers"
```

注册表守卫（v2 明确，评审 A-C5）：driver **不进** `_ALL_NODES`/`__all__`/pyproject 节点段；
新增独立断言——组内每个 entry_point 可 `ep.load()`、满足 SandboxDriver Protocol、
**加载失败降级告警不炸 `import plaita_nodes`**；driver SDK 懒 import（同 agentproc 先例
agent_run.py:130-134），e2b SDK 走 optional-dependencies extra。

## 4. 配置形状（v2：定义权收归 infra）

**裁决（评审 D-B1）**：`workspaces` 定义不放 flow YAML。console 存在运行时 flow 写入路径
（POST /flows、PUT version、import、ai-generate），flow 作者获得 driver/image 选择权等于
获得 infra 级权限（image = 往宿主 docker daemon 调度任意代码）。

- **infra 注册表 `.plaita/sandboxes.json`**：沿用 `config.py` 的搜索顺序（`~/.plaita →
  <repo>/.plaita`）与深合并；字段：`driver`、`image`（**digest pin**）、`template`、
  `provision`（`git.repo` 常量 / `empty`）、`resources`、`env`（**plaita.credentials 引用名**，
  禁明文）、`egress` 档位。
- **节点字段不变**：

```python
class AgentRunNode(Node):
    agent: Optional[Any] = "glm-52"
    prompt: Optional[Any] = None
    repo: Optional[Any] = None        # 保留：宿主直跑路径
    workspace: Optional[Any] = None   # 新增：注册表内的 workspace 名（可表达式）
```

- `workspace` 与 `repo` 互斥（同现报配置错误；构建期 `validate()` 钩子做早期可见性——
  JSON 直载路径是 warning 降级，运行期硬守卫仍是必需）；
- **求值语义（规格，回应评审 A-B2）**：spec 中的 `provision.git.branch` 允许表达式，在
  **首个引用节点的 execute 内**用该节点的 `execution.evaluate` 求值；spec 存于
  globalContext（进 `$GLOBAL`，checkpoint 全量序列化，恢复后原始 spec 仍在）；
  ensure 幂等 ⇒ 先求值者定终身，恢复重算同名 attach。**作者约束：spec 表达式必须确定性
  （禁 `$F.now()` 等），否则按名重派生不成立**；
- **fan-out 命名修正**：v1 示例的 `$i` 不存在（会 KeyError），正确写法是
  `workspace: "task-{% $LOOP-INDEX %}"` 或 `"task-{% $INPUT.index %}"`；
- **None 静默守卫**：表达式求值结果必须为非空字符串，否则**硬失败**（`$INPUT` 缺键的历史
  语义是静默 None → `task-None` 会让全部迭代共享同一沙箱，事故级）；
- **未注册 workspace 名 fail-closed**：运行期硬拒，不自动创建；
- `repo` 参数在 workspace 模式下仍传给 `resolve_agent` 以保留 `<repo>/.plaita` 配置搜索
  目录；仅 `RECURSIVE_WORKSPACE` 重映射为 `handle.path`（沙箱内路径）；
- 节点输出追加观测快照：`"workspace": {ws_key, driver, id, path, env_names(不含值)}`——
  同时是 flow 结束释放集合与 reaper/审计的数据源（§6.3）。

## 5. Driver 一览（v2 修正击杀与注入语义）

| | ensure | 执行路径 | 数据层 | 击杀 | 要点 |
|---|---|---|---|---|---|
| `docker` | volume 准备；**无常驻容器**（per-exec 一次性容器，`--name plaita-ws-{execution_id}-{ws_key}` 确定性派生） | runner Popen(`docker run --rm -i --name <派生名> … timeout <T> …`) | named volume | 沙箱内 `timeout` 墙钟 → driver 按派生名 `docker kill` + `rm -f` | sig-proxy **仅覆盖 INT/TERM**，超时路径不可依赖（executor 超时走 SIGKILL，runner.py:541-548, 624-628）；env 经 `--env-file`（0600 即焚），禁 `-e VAR=VAL`（密钥进 argv，ps 可见）；git 数据面每次经独立的短命 `docker run` 执行 |
| `krunvm`（**实验档**，本地 microVM：libkrun/Hypervisor.framework，已试点） | 宿主数据目录 + `krunvm create`（OCI→microVM，确定性 VM 名） | runner Popen(`krunvm start <vm> -- timeout <T> …`) | 宿主数据目录（bind mount 进 VM） | **VMM 在调用进程内**——宿主 killpg 连 VM 一起死，天然无孤儿；`enforce` 按名 pkill 兜底 | git 数据面在宿主侧（密钥零入 VM；数据面不隔离的实验档口径）；macOS 需大小写敏感 APFS 卷 + `krunvm list` 首跑；VM 配置文件竞争 → v1 单 worker 假定；无 ENTRYPOINT 干扰（比 docker 简单） |
| `ssh`（**实验档**，已落地） | 远端 `mkdir -p` + clone（数据面同盘，远端需可达 repo） | runner Popen(`ssh … "cd <wsdir> && timeout <T> <quoted argv…>"`——远端命令串 POSIX 转义，参数边界不丢） | 远端 VM 盘（与计算层同盘） | 沙箱内 `timeout` 墙钟 → `enforce` 按 wsdir `pkill -f`（`[/]` 括号技巧防自匹配；会话壳刻意不带 `exec` 以保住标记） | 远端命令串携带 wsdir 作 pkill 标记；不做 env 注入（凭据应在远端配置，token broker 后续）；无挂起期释放（同盘）；reaper 远端枚举 v1 恒空（需连接面注入） |
| `e2b`（API 型代表） | API 建沙箱 + clone | **分叉循环**（§3.2） | git remote（snapshot 可选） | 平台 kill process | ensure 结果按 `(execution_id, ws_key)` 缓存于执行内存——**只是加速，正确性只依赖幂等 attach**（重投可能换 worker） |

超时参数关系：runner `timeout_secs` = 沙箱墙钟 `T` + 余量（宿主侧兜底）；`T` 计入
`resources.timeout`。provision 耗时在 ensure，不吃 agent 超时。

## 6. 生命周期与可靠性（v2：补齐挂起态与 workspace lease）

### 6.1 四态生命周期

```
ensure → exec → 挂起（hitl_await / EventNode 等 `is_suspending` 节点，可跨数天；
                gate 与阻塞版 hitl 不挂起）→ release
                     │
                     └─ on_flow_suspend（checkpoint.md:37 证实钩子存在）：
                        dirty-check（driver.git status --porcelain）
                        ├─ 干净 → release(keep_data=True)   # 计算层停费，数据层在
                        └─ 脏   → 强制 commit+push 到 plaita/wip/{execution_id}/{ws_key}
                                   再 release（不靠 prompt 约定的自觉）
```

- `SandboxLifecycleCallback` 随仓发布（plaita-nodes 不构造 FlowExecution，回调由部署侧
  注册——flow_worker `callback_handlers` / 普通模式构造参数），**best-effort**；
  **status-aware reaper 才是主兜底**，如实摆正两者地位；
- release 句柄集合从 context 各节点输出快照 `$NODE.*.workspace` 重建（崩溃后依然可得）。

### 6.2 per-driver 挂起重建语义（如实表）

| driver | 挂起释放计算层后 rebuild 拿回 |
|---|---|
| docker | named volume → 工作副本**完整找回** |
| e2b | git remote → **push 过的**；未 push 工作副本、CLI session 丢（snapshot 启用则可保） |
| ssh | **同盘，物理不可分**：v1 不提供挂起期释放（文档明示持续计费/占用），或标注「释放即丢」 |

### 6.3 status-aware reaper（v2 修正：v1 的 `end_time IS NULL` 谓词是错的）

代码事实：挂起落盘 `status=suspended` 且**无 end_time**（flow_worker.py:417-420），
`end_time IS NULL` 会把等人审批的合法执行当孤儿回收。修正：

- 僵尸判定：`status='running' AND last_update_time 超阈值`——这是对 flow-worker.md:21
  现行「`end_time IS NULL` 巡检」运维口径的**纠正**（该口径未区分 suspended，本方案
  不沿用；后续应反向同步 docs-site）；
- `status='suspended'` **永不回收**（或挂起超天级、显式可配置才回收）；
- 终态孤儿（终态落盘后、回调前崩溃）：按 handle 命名前缀枚举 driver 侧资源反查执行状态；
- ssh driver 无枚举面 → reaper 承诺仅限 docker/e2b，ssh 依赖 `enforce`+TTL 标记文件。

### 6.4 超时三段击杀（v2 修正）

1. 沙箱内 `timeout` 墙钟（第一击杀权，宿主客户端死活无关）；
2. 节点侧捕获超时（executor 路径表现为非零退出 + EXIT_TIMEOUT error 串）→ 调 driver
   `enforce(force=True)`；
3. runner 的 killpg/SIGKILL 只影响宿主客户端，**不承担沙箱内击杀**——conformance 加
   「宿主进程组被 SIGKILL、客户端已死 → 沙箱内无孤儿」场景（§10.3）。

### 6.5 workspace 级 lease（v2 新增，回应评审 C-B3）

execution lease 只在节点间续约（flow_worker.py:426），TTL 120s vs agentrun 超时 1800s
= 15 倍裸奔窗口；lease 过期后第二 worker 从 checkpoint 重放该节点 → 同一 workspace 两个
agent 并发 exec。运行期串行守卫（结构级）管不住这种时序并发。裁决：

- 键 = `handle.id`，`ensure/exec` 前 `SET NX` 获取，exec 期间由 wrap 层心跳续约，
  TTL 覆盖沙箱墙钟 + 余量；
- **冲突落地语义（v2.1）**：抢不到 → 节点抛 `SandboxLeaseError` → execution 进 error
  终态（worker 对普通异常不自动重投），错误信息指引导出重投/DLQ 回灌。不在 plaita-nodes
  侧 import `plaita.server` 的租约异常类型（守住「只依赖 Node/NodeExecutionContext 窄
  接口」的仓规）——如需自动重投语义，向 plaita 另行协商；
- **抢占清残（v2.1）**：重投 worker 以**非续约方式**拿到 lease（上一位持有者已死）时，
  exec 前先对 handle 执行一次 `enforce`——防其沙箱内墙钟未到点、残留容器仍在写同一
  volume 的 crash-only 窄窗口；
- 实现落在 sandbox.py（Redis 复用 plaita.server.execution_lease 同款机制），plaita core
  零改动；
- 结构级守卫保留：同一 `workspace` key 禁止出现在并行分支（编译/加载期 warning +
  运行期拒绝）。

### 6.6 幂等与 exactly-once 边界（v2 明示）

- 产物侧：push 分支 `plaita/{execution_id}/{node_id}`、PR/评论幂等键；
- 相关性 env：向沙箱注入 `PLAITA_EXECUTION_ID` / `PLAITA_NODE_ID`（白名单机制内），
  prompt 侧幂等键有确定来源；
- **明示**：沙箱化不提供 exactly-once。agent 在沙箱内发的外部 API 请求重复 exec 即重复
  生效，git 幂等键帮不了；敏感外部动作须置于 HITL 门后或幂等 API
  （对齐 flow-worker.md「副作用仍须幂等」口径）。剩余保护：workspace lease 并发排除、
  `max_deliveries` 后进 DLQ 封顶、ensure-attach 防克隆分叉；
- start 重投（新 execution_id）→ 新 workspace fresh clone，语义正确且**比现状干净**
  （现状 repo 直跑重投 = 在脏宿主目录重跑）；旧 execution workspace 由 reaper 闭环。

### 6.7 checkpoint 卫生

- 上下文只放句柄/观测快照（`{ws_key, driver, id, path, env_names}`，极小）；
- **observations 漏水管（v2 补）**：`details=true` 输出最多 50 条**全量** tool
  input/output（agent_run.py:66 ` _DETAILS_CAP=50`，无字节上限），沙箱工作流的 tool
  结果恰是 cat 文件 / git diff——正是声称留在 git 的东西。裁决：沙箱模式下 observations
  加**字节截断**（默认 512B/条，可配），超限条目以 git blob 引用替代；
- loop 内挂起恢复的 handle 稳定性进测试计划（§9）。

## 7. 安全（v2：从清单到机制）

1. **canary 脱敏层**：注入 env 的全部 value 生成 canary 副本，exec 边界对 on_line 行、
   ExecResult.stdout/stderr、错误信息三处扫描，命中替换 `[REDACTED:<VAR_NAME>]`，
   之后才进 checkpoint/观测/日志。仓规措辞落成可判定形式：**注入 env 的值不得出现于
   日志/观测/checkpoint/错误信息**。
2. **token 生命周期**：git token 按 execution 签发，TTL ≤ 节点超时，`release()` 即吊销；
   凭据一律经 plaita.credentials 名引用（Fernet 加密），禁明文/`${VAR}` 进 YAML；
   credentials 文件路径运行期强制绝对路径（默认相对 CWD 的 `.plaita-credentials.json`
   有随数据层被 push 的风险）。
3. **egress**：docker driver 默认 egress 白名单档（git remote 域 + 包管理源）+「无网」档；
   ssh/e2b 在文档写明平台侧控制与残余风险。clone 进来的 repo 是攻击载荷载体
   （§1.3），egress 是对此的真实对策。
4. **run 画像**：禁 `--privileged` / `--network host` / `--pid host` / 额外 caps；
   `docker.sock` 永不入沙箱。
5. **供应链**：image/template digest pin，未 pin 拒绝 ensure（修正 v1 示例自用的
   `latest`）；注册表可配白名单替代。
6. **ssh**：私钥与 host key 预置进 plaita.credentials（否则 strict host key 退化成 TOFU）；
   `authorized_keys` 用 `restrict,command=<fixup>,no-port-forwarding,no-agent-forwarding`。
7. **dry_run 可证伪**：dry 判定先于 workspace 解析**和**凭据/env 组装（monkeypatch
   `get_credential`/`resolve_agent` 为 raise，dry 下不得触发）；RecordingDriver 计数断言
   `ensure/exec/release` 调用数为 0（含回调 release 必须 no-op）；节点级与 globalContext
   双 dry 路径都测；兜底 monkeypatch subprocess.Popen/socket。
8. driver 模块禁 DEBUG 日志（SDK/httpx DEBUG 会打请求头），canary 层兜底。

## 8. 兼容与迁移（v2 修正）

- 无 `workspace` 字段的存量 flow：行为与今天逐字节一致；
- agentproc：**不动 wire protocol**；EXECUTORS 注册与 `build_args` 签名照旧（本地 argv 型）；
  executor 接口如需扩展（委托执行器），走 spec doc revision + SDK bump 立项（§1.7）；
- API driver 分叉循环的等价性由 conformance 看护（§10.5），不复制协议实现——复用
  runner 导出纯函数；
- `recursive_stream_turn`（console 生成后端）**移出范围**：宿主直跑、自带 Popen 且无
  进程组/超时击杀，无沙箱诉求，wrap 层明确不覆盖；
- wrap 层替换 executor `install_hint`（沙箱模式下缺的是 docker/ssh 客户端，不是 CLI）；
  `shutil.which(argv[0])` 校验的是 driver 客户端——API driver 路径由 driver 侧等价承接
  （image/template 存在性校验）；
- plaita core 零改动（挂起回调/reaper/lease 全在 plaita-nodes 侧）。

## 9. 测试与验收（v2 增补）

1. 单测（fake driver）：argv 组装、命名派生、`workspace`/`repo` 互斥、未注册名 fail-closed、
   None 静默守卫、dry_run 零调用（§7.7 全套）、ensure 缓存、lease 抢占/快速失败；
2. driver conformance 套件（§10）：docker 实跑全过；ssh 标注实验性单独档；e2b 凭环境变量跳过；
3. 挂起恢复专项：loop 内 agentrun 后挂 hitl → 恢复 → 断言 handle id 不变且 attach 命中；
   挂起期 dirty-check → wip push → rebuild 后 working tree == wip ref；
4. 击杀专项：killpg SIGKILL 宿主客户端后，沙箱内进程在墙钟到点被 `timeout` 终结，
   `enforce` 后无残留；
5. e2e：argusai 跑「clone → agentrun(沙箱) → push → 断言」闭环；
6. 注册表守卫：entry_point 可加载 / Protocol 满足 / 加载失败降级告警。

## 10. Driver 契约九条（v2 重写，全部可证伪）

1. `ensure` 幂等：同 id 二次调用 = attach；
2. handle 可重派生：execution 上下文丢失后按 `(execution_id, ws_key)` 重连；
3. 三段击杀：沙箱内墙钟生效；宿主进程组被 SIGKILL、客户端已死 → 墙钟仍终结进程树；
   `enforce(force=True)` 后无残留进程/容器；
4. **脱敏**：注入 `CANARY_TOKEN` 后，exec 返回/回调的任何字节出现 canary 即 fail；
5. **wire 等价**（API driver）：与 runner 路径在同一 fake agent 输入下 RunResult 逐字段相同；
   本地 argv 型 driver：行级收集语义与 runner 一致（`on_protocol_line` 为返回后补发，
   非实时流——文档措辞统一为「行级收集」）；
6. 数据进出只经 git（`git()` 白名单方法外无文件传输面）；
7. dry_run 零调用（RecordingDriver 计数 + dry 先于凭据组装，§7.7）；
8. 供应链：未 pin（digest/白名单）的 image/template → ensure 必须失败；
9. **release→ensure 重建后 working tree == 最后一次 push 的 ref**（每 driver 显式声明
   丢失面：docker=无 / e2b=未 push 部分 / ssh=实验性同盘）。

## 11. 与既有节点的配合（v2：机制替代约定）

- **出活 push 的机制化**：不依赖 prompt 自觉——挂起/结束路径的 dirty-check + 强制 wip
  push（§6.1）是兜底；正常出活仍由 agent 按约定 push（效率路径）。独立薄
  `sandbox_exec` 节点（经 `driver.git` 跑 push）作为 P3 可选项，v1 不做；
- **git_publish 绊线**：flow 同时存在 workspaces 定义（或任何节点输出 workspace 快照）
  与宿主侧 `git_publish` 时，spec 求值期打 runtime warning——防「writer 迁到沙箱、
  出害口还指着宿主遗留 checkout → 各自成功、活儿永不出仓」的静默分叉
  （git_publish 现状：宿主 subprocess、dry 下假成功 pushed=True，git_publish.py:83-91）；
- hitl / gate 走宿主 EventBus，零改动；
- `writefile` 等宿主文件节点与沙箱 workspace 不可混用于同一份代码（文档明示）。

## 12. 实施计划（v2 调整）

> **状态（2026-10-01）**：P1 已落地——`plaita_nodes/sandbox.py`（协议/注册表/
> sandboxes.json 解析/WorkspaceLease/Redactor/envfile/wip 纪律）+
> `plaita_nodes/sandbox_docker.py`（docker driver）+ AgentRunNode `workspace` 字段 +
> 57 个单测（tests/test_sandbox.py、tests/test_agent_run_workspace.py），全量回归绿。
> **docker E2E 已实跑通过**（tests/e2e_sandbox_docker.py，3 条：真容器节点往返 /
> workspace 跨节点延续 / git 数据面 clone→脏区→wip push→重建断言；无 docker 环境
> 自动 skip）。E2E 实测修正两处镜像契约：包裹 argv 显式 `--entrypoint timeout`、
> driver 数据面显式 `--entrypoint git`（镜像 ENTRYPOINT 不参与执行链）；wip commit
> 显式 git 身份。
> **krunvm 本地 microVM driver 已实验性落地**（`sandbox_krunvm.py`，libkrun/
> Hypervisor.framework；本地试点经 `brew tap slp/krun && brew install krunvm`，
> macOS 需大小写敏感 APFS 卷）：真 microVM E2E 三条已过（tests/e2e_krunvm_sandbox.py：
> 节点往返含 exit code 透传 / git 数据面宿主侧 wip 纪律 / 失败 enforce）；抽象层
> `wrap_agent_argv_from_env` 按 `PLAITA_SANDBOX_DRIVER` 分派。
> **P2 前半已落地**：`lifecycle.py`（SandboxLifecycleCallback，挂起/结束 best-effort
> 释放：dirty-check → wip push → release，部署侧注册）；`sandbox_reaper.py`
> （status-aware reaper CLI：running 超阈=僵尸 / suspended 永不 / 终态孤儿回收；
> 资源枚举经 docker volume labels + krunvm 数据目录 sidecar）；
> `tests/test_driver_conformance.py`（契约 1/2/3/6/8 实跑套件 × docker/krunvm，
> 含「宿主 SIGKILL 后无孤儿」场景）；git_publish 沙箱绊线（§11）；
> krunvm guest 出网实测（HTTPS/DNS 可出网、ICMP 不通 → egress 白名单对 krunvm
> 为 P2 必需项）。
> **P2 后半（部分）已落地**：`sandbox_ssh.py`（ssh driver，实验档——远端 VM 数据
> 面 / `shlex` 转义保参数边界 / `[/]` 括号防 pkill 自匹配；E2E 以 Docker sshd 为
> 靶机 4 条已过，tests/e2e_ssh_sandbox.py）；authoring-spec 增补 §5.2 沙箱作者
> 约束。**P3 前置里程碑已落地**：`tests/e2e_real_agent_sandbox.py`——真实
> recursive CLI（linux-musl 发布物，SHA256 校验）+ 真实 provider 凭据 + git daemon
> provision，在 docker 沙箱内完成真实 LLM 工具循环并编辑仓库（argusai 全系统 E2E
> 的前置形态）。本轮实测驱动三项修正：数据层 named volume → 宿主数据目录（colima
> 具名卷被守护端静默销毁）；git 数据面统一 `-c safe.directory=*`（bind mount 属主
> 映射）；EXEC_ID 会话唯一（virtiofs 对删后重建同名路径有陈旧缓存）。
> **待办**：e2b driver（分叉循环 + wire 等价断言，需云端凭据实跑）；token 按
> execution 签发 + release 吊销（需凭据签发方，如 GitHub App）；egress 白名单强制
> （Linux iptables / Docker 网络策略，macOS 宿主无法实测）；agentproc「委托执行器」
> spec 立项；argusai 全系统 E2E（多节点 flow 编排形态）。

| PR | 内容 | 验收 |
|---|---|---|
| P1 | `sandbox.py`（协议 + 注册表 + `.plaita/sandboxes.json` 解析 + workspace lease + canary 脱敏 + dirty-check）+ `docker` driver + AgentRunNode `workspace` 字段 + 单测 | 单测全绿；docker 过 conformance 1-9 |
| P2 | conformance 套件落地 + 结构级串行守卫 + git_publish 绊线 + `ssh` driver（实验性档） | docker 全过；ssh 实验档标注；挂起恢复专项过 |
| P3 | `e2b` driver（分叉循环 + wire 等价断言）+ 文档同步（AGENTS.md / README / authoring-spec）+ argusai e2e + console publish 门禁校验 | e2e 闭环；文档门禁过 |

## 13. 明确不做（Non-goals，v2 增补）

- 不做文件传输 API（git 即协议）；
- 不做跨机 workspace 池化 / 预热池；
- 不做 plaita core 编译期校验（运行期守卫 + 加载期 warning 先顶）；
- 不在本方案内改 agentproc spec（委托执行器扩展另立项）；
- 不在 v1 支持同一 workspace 内多 agent 并发（结构守卫 + workspace lease 双保险）；
- 不覆盖 `recursive_stream_turn`（宿主直跑生成后端，无沙箱诉求）；
- v1 不为 ssh 提供挂起期释放（同盘不可分，明示持续占用）。

## 14. 开放问题（v2 裁决后剩余）

- **Q1**（已裁决框架）reaper = status-aware + 终态孤儿反查；**剩余**：suspended 超天级
  回收的默认天数（建议 7d，可配）；
- **Q2**（已裁决）不做独立 `workspace_release` 节点，回调 + reaper 足够；
- **Q3**（已裁决）workspace 级 lease，快速失败不排队（§6.5）；
- **Q4**（已裁决）出活 push = 约定 + dirty-check 机制兜底；薄 `sandbox_exec` 节点 P3 可选；
- **Q5**（已裁决）快照字段 = `{ws_key, driver, id, path, env_names}`（§4）；
- **Q6**（已裁决）agents.json agent 级 sandbox 块后置，有真实工具链差异需求再立；
- **Q7**（新增）agentproc「executor 委托外部执行器」扩展的立项时点——分叉循环是过桥，
  长期应收编；
- **Q8**（新增）egress 白名单默认域名集合：部署期配置（git remote 域 + 包源），不开运行时
  配置面。

## 15. 评审记录（2026-10-01，四路交叉）

| 评审视角 | 结论 | 关键 Blockers | v2 采纳 |
|---|---|---|---|
| plaita/plaita-nodes 集成 | APPROVE_WITH_CHANGES | 执行权归属矛盾；`$i` 不存在 + None 静默 + 求值时序未定义 | §1.6/§3/§3.2/§4 |
| agentproc 执行语义 | REQUEST_CHANGES | e2b 无 argv 消费者（架构图与 §8 自相矛盾）；executor 超时是 SIGKILL，sig-proxy 救不了 | §2/§3.2/§5/§6.4 |
| 可靠性/断点续跑 | REQUEST_CHANGES | 挂起期生命周期缺失；reaper 谓词误杀挂起；lease 15 倍裸奔窗口需 workspace 级 lease | §6 全节 |
| 安全 | REQUEST_CHANGES | workspaces 定义权收归 infra；canary 脱敏层；token TTL + egress | §4/§7/§10 |

评审采纳原则：10 条 Blockers 全部落进正文（对应小节见表）；Concerns 按「可证伪、
有 owner、进契约」标准采纳（脱敏、lease、供应链、dry_run 可证伪、observations 截断、
git_publish 绊线、ssh 降实验档等）；Nits 择要采纳（措辞、行号引证、示例修正）。
