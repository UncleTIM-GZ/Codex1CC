# Codex1CC 产品需求文档

**版本：** PRD v1.0（待评审）  
**产品定位：** 在本机将 Codex 的任务委托给 Claude Code CLI，并让 Codex持续接收进展、处理问题、审查结果和决定是否续接。  
**计划仓库：** `https://github.com/UncleTIM-GZ/Codex1CC.git`  
**计划本机目录：** `\\wsl.localhost\Ubuntu-22.04\home\timou\repos\Codex1CC`，对应 WSL 路径 `/home/timou/repos/Codex1CC`

> **仓库现状说明：** 当前核查环境中 `/home/timou/repos/Codex1CC` 尚不存在；提供的 GitHub 地址也未能通过公开网页访问。因此本 PRD 使用你指定的地址和路径作为目标，**不声称已检查仓库内容或已将文档写入仓库**。

## 1\. 背景与目标

当前 Codex 可以调用本机 MCP 工具，Claude Code CLI 可以通过非交互模式执行任务、输出 JSON 事件并按会话 ID 续接。Codex1CC 要把两者连接起来，使较长的 Claude 任务不会占住一次 MCP 调用。

目标工作流：

```
用户提出需求
  → Codex 判断并提交明确任务
  → Codex1CC 后台运行 claude -p
  → Codex 轮询进展并回答任务中的问题
  → Claude 继续执行
  → Codex 审查结果、决定续接或完成
```

**核心成功标准：** 提交任务后立即获得任务 ID；Codex 的 MCP 连接刷新或断开时，后台任务仍可运行；Codex 恢复后可读取进展，并凭明确的 Claude 会话 ID 续接。

## 2\. 产品边界

### v1 必须完成

- 本机单用户、单机运行。
- Codex 通过本地 stdio MCP 调用 Codex1CC。
- Codex1CC 使用 **`claude -p` CLI 子进程**执行任务，**不使用 Claude Agent SDK**。
- 后台任务、事件、会话映射和用量持久化。
- 每个目标项目同一时间最多运行一个 Claude 任务。
- 支持问题回传、回答、续接、取消和 Codex 审查状态。
- 默认严格限制 Claude 的工具、文件范围和费用。
- 提供明确的停用及故障定位办法。

本机与[官方 CLI 参考](<https://code.claude.com/docs/en/cli-reference>)确认的参数是小写 `-p`／`--print`；本文统一使用 `-p`。

### v1 不包含

- 后台自主运行 Codex 模型循环或主动唤醒已结束的 Codex 会话。
- 多用户、跨机器和公共网络服务。
- 自动提交、推送、建 PR、部署、安装依赖或清理用户文件。
- 无上限的自主长程迭代。
- 把现有 OCN 仓库默认当作 Claude 的工作项目。

## 3\. 目标用户与使用场景

**目标用户：** 在同一台机器上使用 Codex 与 Claude Code、希望由 Codex 统筹并审查 Claude 执行结果的开发者。

主要场景：

1. Codex 将范围明确的实现或分析任务交给 Claude。
2. Claude 执行数分钟至一小时；Codex 分批读取事件，而不等待单次 MCP 调用结束。
3. Claude 遇到技术选择时向 Codex 提问，Codex依据原需求回答。
4. Claude 一轮结束后，Codex检查文件、结果及验收标准，再续接或标记完成。
5. 用户取消任务，Codex1CC 只结束该任务拥有的进程，并保留现场。

## 4\. 本机基线与部署约束

| 项目 | 已核查结果 |
|---|---|
| 系统 | WSL2 Linux，x86\_64；Shell `/usr/bin/zsh` |
| Codex CLI | 0\.158.0；入口 `/home/timou/.npm-global/bin/codex` |
| Codex 内置二进制 | `/home/timou/.codex/packages/app-server-daemon/releases/0.158.0-x86_64-unknown-linux-musl/bin/codex` |
| Claude Code | 2\.1.283；实际文件 `/home/timou/.local/share/claude/versions/2.1.283` |
| Python | `/usr/bin/python3.10`；系统解释器已有 `mcp 1.26.0` |
| Node | `/usr/bin/node`，v20.20.0 |
| 候选文件沙箱 | `/usr/bin/bwrap` 已存在，**能否用于本项目尚未验证** |
| Codex 配置 | `/home/timou/.codex/config.toml`；未见 `claude_executor` 名称冲突 |
| Claude 配置 | 用户设置含认证字段，配置了 DeepSeek 兼容端点和模型覆盖；实际请求路由及计费未验证 |

**项目区分：** `/home/timou/repos/Codex1CC` 是桥接程序的开发仓库；供 Claude 执行任务的**目标项目**须另外由用户指定并配置允许范围。当前 `/home/timou/repos/OCN` 只有部署会话工作目录这一身份，不自动获得授权。

**建议技术选择：** Python 3.10、独立虚拟环境、官方 MCP Python SDK、SQLite 状态库、Unix socket、本机 Claude CLI。Python 用于桥接程序和 MCP 服务；Claude Agent SDK 不进入依赖清单。依赖版本在实施阶段锁定。

## 5\. 总体架构

```
This Mermaid diagram doesn't fit the current terminal width.
flowchart LR
    C[Codex] -->|短时 stdio MCP 调用| M[Codex1CC MCP 前端]
    M -->|当前用户专用 Unix socket| D[后台执行器]
    D --> DB[(任务、事件、会话数据库)]
    D -->|受限子进程| CC[claude -p]
    CC -->|stream-json| D
    CC -->|仅用于提问的本机 MCP| Q[问题处理器]
    Q --> D
```

- **MCP 前端**只验证参数、转发请求、返回有限结果；其退出不结束后台任务。
- **后台执行器**拥有任务调度、进程、事件、超时和恢复记录。
- **Claude 子进程**通过绝对路径启动，不经 Shell 拼接命令。运行参数固定包含 `-p`、`--output-format stream-json`、显式工作目录及限制配置。[官方程序化运行说明](<https://code.claude.com/docs/en/headless>)确认 `stream-json` 是实时逐行事件流，末尾结果含会话及用量元数据。
- IPC、数据库和日志只允许当前用户访问，不监听公共 TCP 端口。

## 6\. MCP 工具契约

所有工具返回稳定的 `task_id`、状态和结构化错误码。写操作需接受 `request_id` 作为去重键。

| 工具 | 主要输入 | 主要输出 | 要求 |
|---|---|---|---|
| `submit_task` | `project_id`、任务文本、验收标准、`request_id` | `task_id`、排队状态、事件游标 | 立即返回；重复 `request_id` 返回原任务 |
| `get_task` | `task_id`、`cursor`、`wait_ms` | 增量事件、最新状态、下个游标 | `wait_ms` 最大 20 秒；结果分页和截断标记 |
| `respond_task` | `task_id`、`question_id`、答案、`request_id` | 已接受状态 | 重复回答幂等；过期问题拒绝 |
| `continue_task` | `task_id`、追加指令、`request_id` | 新轮次 ID、状态 | 仅在上一轮结束且已保存 Claude 会话 ID 时允许 |
| `cancel_task` | `task_id`、`request_id` | 取消结果及现场路径 | 仅结束本任务的进程组；不删除项目文件 |

**错误码至少包括：** `PROJECT_NOT_ALLOWED`、`PROJECT_BUSY`、`TASK_NOT_FOUND`、`QUESTION_EXPIRED`、`INVALID_STATE`、`SESSION_ID_MISSING`、`LIMIT_REACHED`、`CLI_FAILED`、`SANDBOX_UNAVAILABLE`。

### 状态机

```
queued → running → waiting_answer → running
                  ↘ review_required → continuing → running
                  ↘ completed
                  ↘ failed / interrupted / canceled
```

`review_required` 表示 Claude 已结束一轮、等待 Codex 审查。只有 Codex 检查验收标准后，任务才可进入 `completed`。取消、崩溃和正常失败须分别记录。

## 7\. 提问与续接

### 7\.1 结构化提问

纯 `claude -p` 没有 Agent SDK 的 `can_use_tool` 回调。v1 的拟定方案是：在 `--strict-mcp-config` 下仅向 Claude 提供 **Codex1CC 自己的本地提问 MCP 工具**。Claude 调用该工具时，问题处理器保存原始问题和选项、发布 `question_id`，并等待 `respond_task` 交回答案。同一问题只能成功回答一次。

这一路径是**设计方案，不是已验证的 CLI 能力**。实施早期必须验证 Claude Code 2.1.283 能否在受限配置中调用该工具，并在等待期间保持原执行上下文。验证失败则不得声称结构化问答闭环通过。

Claude 的内置 `AskUserQuestion` 不作为 v1 的已证明接口；不得从普通输出中猜测它已被正确处理。

### 7\.2 普通文本提问

Claude 若以普通文本提出问题并结束当前轮，Codex1CC保存完整输出并进入 `review_required`。Codex 阅读后用 `continue_task` 回答，后台执行器通过保存的会话 ID 调用 `claude -p --resume <id>`。不得靠问号或关键词自动判定是否需要回答。

### 7\.3 权限请求

权限请求与业务问题使用不同事件类型。技术问题可由 Codex 在既定授权范围内回答；答案**不能改变允许目录、工具集合或费用额度**。扩大权限、额外付费和不可逆操作必须由用户另行决定。

## 8\. 安全与文件范围

1. 默认仅允许目标项目中明确列出的文件或目录；首次真实验收进一步限制为只读。
2. 使用 `--restricted`、`--strict-mcp-config`、明确的 `--tools` 列表及专用配置。`--allowedTools` 只影响免提示授权，**不能充当工具白名单**；应使用 `--tools` 限定可用内建工具。[官方 CLI 参考](<https://code.claude.com/docs/en/cli-reference>)
3. 默认禁用 Bash、REPL、浏览器、WebFetch、外部 MCP、子代理和执行型工具；仅保留必要的读取工具及 Codex1CC 内部提问工具。
4. 编辑阶段的每次路径检查须处理绝对化、符号链接、目标不存在时的父目录解析和竞态。仅凭 `cwd` 或提示词不足以形成文件沙箱。
5. 在启用编辑前，必须验证可用的 OS 级隔离；本机虽有 `bwrap`，但当前只确认**可执行文件存在**。若隔离或强制检查不能满足范围要求，编辑功能保持关闭。
6. 不向 Claude 继承现有的 8 个全局 MCP、40 个插件和宽泛权限。只传入运行所需的认证、服务地址及模型配置；令牌不得出现在命令行、事件或日志中。
7. 后台程序和状态目录位于目标项目之外；Claude 不得修改桥接程序、其配置、`.git` 或凭据文件。

## 9\. 生命周期、持久化与费用

- 每轮默认最长 60 分钟；等待回答时间单独计时。等待超过 24 小时，释放 Claude 进程并保留可审查记录；能否在不丢失问题上下文的情况下恢复，属于验证项。
- MCP 断线不取消任务。后台执行器重启后将不确定状态标为 `interrupted`，**不自动重放**可能已修改文件的任务。
- 取消采用进程组终止和限时强制终止，并记录退出码、最后事件及可能的部分改动；不清理用户文件。类似的进程组处理可参考[claude-code-agent-for-codex 源码](<https://github.com/Qihao-Duan/claude-code-agent-for-codex/blob/main/server.py>)。
- 保存每轮 CLI 报告的 token、成本字段及会话累计观察值。`--max-budget-usd` 只用于**单次调用估算上限**；[官方参考](<https://code.claude.com/docs/en/cli-reference>)说明续接时先前用量不计入该次上限。
- 本机使用 DeepSeek 兼容端点；其价格、CLI 成本字段及硬额度是否准确均未验证。显示“未知”或“估算”必须与实际账单区分。正式自主长程迭代在用户确认硬额度前关闭。

## 10\. 非功能需求

| 类别 | 验收要求 |
|---|---|
| 响应 | `submit_task` 在完成参数检查后立即返回；`get_task` 单次最多等待 20 秒 |
| 隔离 | 不开放公共端口；socket、数据库、日志仅当前用户可访问 |
| 可靠性 | 重复提交与重复回答无副作用；MCP 重连后可按游标读取既有事件 |
| 可观测性 | 每个任务可查状态、时间、轮次、Claude 会话 ID、退出原因、用量及脱敏日志 |
| 数据保护 | 不记录令牌；事件和结果设置大小上限、分页与保留期 |
| 兼容 | 启动前检查所需 Claude CLI 参数；版本能力不满足时明确失败 |
| 回退 | 只停用 Codex1CC 注册项及本次创建的进程和文件；不重置目标仓库 |

## 11\. 开发与验收顺序

| 阶段 | 交付物 | 通过条件 |
|---|---|---|
| P0：仓库与能力确认 | 仓库基线、目标路径、CLI 兼容报告 | 能读取指定仓库；确认实际版本、认证和隔离能力 |
| P1：本地模拟 | 后台执行器、数据库、五个 MCP 工具 | 模拟覆盖断线、去重、排队、游标、续接、超时、取消与重启 |
| P2：CLI 接入 | `claude -p` 事件解析、会话映射、限制配置 | 正确解析流式事件和终态；失败不误报完成 |
| P3：提问闭环 | 内部提问工具与回答路由 | 问题挂起、Codex 回答、原轮次继续；过期/重复回答正确处理 |
| P4：Codex 注册 | 增量 MCP 配置 | 当前客户端能发现并调用五个工具，刷新方式实测 |
| P5：极小真实验收 | 一次只读任务、一次提问、一次续接 | 读取一个非敏感文件，完整问答及续接成功，无业务代码改动 |
| P6：交付 | 验证报告、停用和故障定位文档 | 未通过项明确标记；停止继续执行 |

真实模型验收仅限上述极小任务，不自动重试。长时间运行和异常恢复主要通过模拟测试。

## 12\. 参考项目与取舍

- [Qihao-Duan/claude-code-agent-for-codex](<https://github.com/Qihao-Duan/claude-code-agent-for-codex>)：参考 `claude -p`、流式解析、异步作业和会话续接。
- [ben-gt/claude-code-bridge](<https://github.com/ben-gt/claude-code-bridge>)：参考持久化任务、每项目互斥、日志游标和取消；其自动建分支、提交、推送及部署能力不进入本产品 v1。
- [thomaswitt/mcp-agents](<https://github.com/thomaswitt/mcp-agents>)：参考提交、查询、结果分页和取消的工具契约；Codex1CC 另需解决 MCP 断线后的任务存活。
- [zhendalf/claude-mcp](<https://github.com/zhendalf/claude-mcp>)：参考明确 ID 续接；其绕过权限及断线清理策略不适用。

## 13\. 待确认事项与发布门槛

1. **仓库可访问性：** 当前环境看不到本机 Codex1CC 目录，也无法核实远端仓库内容。开始建项目前须确认其真实状态和访问权限。
2. **目标项目：** Codex1CC 程序仓库与首个 Claude 工作项目要分别指定；需确认允许读取的具体文件。
3. **结构化提问：** 内部 MCP 提问工具能否在本机 CLI 的受限模式中可靠挂起并恢复，必须实测。失败时可交付异步与普通文本续接能力，但**不能发布为完整 v1**。
4. **编辑隔离：** OS 级文件限制与符号链接防逃逸未验证前，仅开放只读。
5. **费用：** DeepSeek 兼容端点的真实计费和 CLI 报告字段未核实；长程自主迭代保持关闭。

**实施授权状态：** 本文是 PRD，尚未克隆仓库、创建文件、安装依赖、注册 MCP 或发起模型请求