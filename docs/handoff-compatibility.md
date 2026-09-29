# Codex 自动接管兼容性与实测记录

记录日期：2026-09-29。这里的结论只适用于列出的组合；其他版本须重新验证。

## 首发连接方式

Codex1CC 连接用户已经启动的 Codex managed App Server daemon，通过其 Unix WebSocket 控制 socket 访问**明确绑定的持久会话**。socket 路径由 `codex app-server daemon version` 返回，程序校验入口及实际目标 socket 的所有者、权限和类型。不会替用户启动宿主 daemon。

初始化时有两种绑定方式：

- 绑定现有 Codex 会话 ID：只做 `thread/read`、`thread/resume` 预检，不产生模型调用。会话必须已有完整历史，并使用 `legacy` history mode。Codex CLI 的状态栏可显示 session ID；桌面端提供“复制 session ID”命令。参见[开发者命令](https://learn.chatgpt.com/docs/developer-commands)与[命令参考](https://learn.chatgpt.com/docs/reference/commands)。
- 显式创建专用会话：`initialize_thread(cwd)` 使用 `thread/start(historyMode=legacy)`，随后发出一条最小初始化消息，等待 `turn/completed`，再从新连接验证 `thread/read` 与 `thread/resume`。**这会产生一次 Codex 模型调用，费用未知。**只创建空会话不够；实测空会话关闭连接后 `thread/resume` 报 `no rollout found`。

官方 [App Server 协议文档](https://learn.chatgpt.com/docs/app-server)描述了 `turn/start` 的 turn ID、`turn/completed`、`thread/read`、`thread/resume` 及历史模式。当前文档说明分页历史会话的完整读取与恢复尚不受支持；因此适配器对 `paginated` 会话拒绝自动绑定。

## 实测矩阵

| 场景 | Ubuntu Linux，Codex CLI / App Server 0.158.0 | 结论 |
|---|---|---|
| managed daemon 可发现与 Unix WebSocket `initialize` | `daemon version` 报 running；WebSocket 收到初始化回执 | 通过 |
| 空会话关闭连接后 `resume` | 报 `no rollout found` | 不支持直接绑定空会话 |
| 已有分页历史会话 | 当前运行中的会话能读取，官方尚不保证卸载后恢复 | 自动模式拒绝 |
| legacy 历史会话创建与持久化 | 最小模型回合后，新连接可读取并 `resume` | 通过；初始化产生一次模型调用 |
| 指定会话启动新回合 | `turn/start` 返回 turn ID | 通过 |
| 不同连接收到结束事件 | `turn/completed` 与原 turn ID 匹配 | 通过 |
| 完成后读取结果 | `thread/read(includeTurns=true)` 返回 completed 和最终答复 | 通过 |
| CC 交付后自动验收 | 临时非敏感项目中 CC 报告 `MAPLE`；Codex 自动调用 `get_task`、`complete_task`、`ack_handoff`，记录结论并退出 `watch` | 通过一次 |
| MCP 调用批准 | `on-request` 下 App Server 请求 `mcpServer/elicitation/request`；仅对绑定任务的验收工具自动批准 | 通过一次真实链路及协议测试 |
| 原窗口关闭后用户能在原窗口直接看到结果 | 未实测 | 不承诺；Codex1CC 任务视图是结果入口 |
| managed daemon 重启时已在运行的回合 | 未重启宿主，以免影响现有会话 | 未验证；结果不明时暂停并对账 |
| 忙碌会话排队后自动启动 | 本地假宿主事件测试通过；真实忙碌窗口尚未实测 | 待真实验收 |
| macOS / Windows | 未实测 | 未支持声明 |

实测使用临时非敏感目录与极短答复。自动验收额外启动一次真实 Codex 模型回合；调试批准机制也启动了有限的真实回合。接口未提供可核实费用数据，因此金额未知。模拟测试不调用模型。

## 接口与恢复规则

适配器公开 `probe(binding)`、`deliver(binding,event_id,prompt)`、`wait_for_turn(binding,turn_id)`、`wait_for_idle(binding)`、`reconcile(binding,turn_id)` 和 `reconcile_event(binding,event_id)`。派发成功返回 turn ID；启动结果不明时通过稳定事件标识查已保存的用户消息。查不到时结果仍是 `unknown`，调用方不得自动重派。

运行中的回合由 `turn/completed` 通知推进，不需要模型轮询。断线后读取保存的 turn ID 与最终状态；会话忙时订阅状态变更，空闲再送下一事件。未知结果、宿主失联或不支持的历史模式都保留为待处理，并显示原因。

用户若需从 Codex CLI 找到现有会话 ID，可在会话的状态栏启用 **session id**，或在桌面端使用“复制 session ID”。具体入口随 Codex 客户端版本变化；绑定时以 `probe` 的实际结果为准。
