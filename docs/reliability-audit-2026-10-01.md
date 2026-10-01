# Codex1CC 0.6.0 可靠性审计

日期：2026-10-01。审计基线：`55e66ed`。

## 发布判断

**原 0.6.0 不满足“长期无人值守稳定版”的验收条件。** 正常闭环已经可用，但进程所有权、终态、恢复监听和升级验证存在缺口。本次直接修复了下列可复现缺陷，并扩充回归与独立安装检查。修复不等于证明永不故障；在真实模型长程运行和宿主重启验收完成前，继续标记 alpha。

本次源码修复尚未发布。现场只停止了已核对无子进程、且不拥有当前服务入口的重复执行器；当前 CC / Godot 门禁继续运行。不得以数据库显示 failed / review_required 就判断可以停止服务。

## 方法与范围

- 阅读执行器、持久状态、宿主协议、授权、工作树、问题桥、安装、CLI、通知和升级模块，以及已有测试与文档。
- 先运行基线测试，再针对生命周期问题写失败测试；第一组 6 项在原实现上全部失败，修复后通过。
- 检查真实执行器、Unix socket 所有者、进程树和只读数据库。未修改项目工作树或真实任务验收结果。
- 用真实 OS 子进程验证超时、管道继承、进程组清理、单实例互斥；用协议对端模拟宿主断连和跨会话事件。
- 从 wheel 安装到全新环境，使用独立空项目配置验证守护服务、MCP 握手、工具参数和 RPC；不调用模型。

## 现场证据与根因

发现两个执行器同时使用默认状态目录，且各自仍有绑定到同一路径的监听 socket。当前可连接入口通过 Linux `SO_PEERCRED` 确认；较新的实例有 CC 与 Godot 子进程，较旧实例没有子进程。

真实任务记录两次 `WORKER_LOST`，随后续派被“Previous worker is still stopping”拒绝，目标因此被标记 blocked；检查时 CC / Godot 实际仍在工作。代码中的 `recover_goals()` 依据**本进程内存中的 jobs/processes**判断工作进程是否存在。两个执行器共享 SQLite 时，一个实例就可能将另一个实例的任务标成丢失。

原启动过程在“检查 socket”和“打开数据库、绑定 socket”之间没有跨进程互斥。它不能保证只有一个执行器恢复、修改同一份任务状态。这与现场双实例和误报的表现一致；没有保留最初启动的完整时间线，因此不将某一个具体启动请求认定为唯一触发源。

## 修复和回归证据

| 风险 | 原行为及后果 | 本次修复 | 主要回归 |
|---|---|---|---|
| P0：双执行器写同一状态 | 各自内存不一致，误报 WORKER_LOST、竞相恢复 | 打开数据库之前取得持有全生命周期的文件锁；第二实例拒绝启动 | `test_concurrent_cold_start_has_only_one_state_owner`、`test_second_daemon_cannot_mutate_live_task_state` |
| P1：失败先释放工作进程所有权 | 清理期间可被续派，旧清理可能覆盖新进程记录 | 由最外层 worker 生命周期统一清理；交付与验收等待清理结束；启动前异常也落库 | `test_failure_keeps_worker_owned_until_cleanup_finishes`、`test_pre_spawn_exception_records_failure_and_releases_job` |
| P1：EOF / stderr 管道绕过超时 | 标准输出关闭或主进程退出后仍无限等待 | 等待实际退出期间继续计时，清理进程组，约束尾部等待；硬墙钟包含等待回答 | `test_closed_stdout_cannot_bypass_hard_deadline`、`test_child_stderr_pipe_cannot_bypass_hard_deadline` |
| P1：取消/失败留下问题 | 桥接调用悬挂，旧超时可覆盖已终止状态 | 终态事务中使问题失效，并唤醒等待桥接调用 | `test_failure_expires_question_and_unblocks_bridge`、`test_cancel_unblocks_pending_question_without_resurrecting_task` |
| P1：收到回执后丢失回合监听 | 重启后没有重新订阅 inProgress，目标永远等待 | 重新订阅已处理但未结束的回合；断连显式 needs_reconcile；不重放不确定操作 | `test_restart_resubscribes_to_handled_but_running_turn`、`test_observer_disconnect_after_receipt_requires_reconciliation` |
| P1：回合/线程占用判断不完整 | 旧轮事件可触发新一轮审查；回执后线程过早释放 | 验证轮号、继续回执必须推进轮次；监听结束前保留线程占用 | `test_stale_round_event_cannot_trigger_a_new_review`、`test_handled_controller_still_reserves_its_thread` |
| P1：跨会话审批互相影响 | 本控制连接可能拒绝另一会话的请求 | 明确属于其他 threadId 的审批消息不由当前连接回答 | `test_foreign_thread_approval_is_not_declined_by_this_controller` |
| P1：目标轮间不占用项目 | 失败待修复期间可派发冲突任务，基线过时 | active 目标在轮间继续参与串行与范围冲突检查 | `test_pending_goal_reserves_project_between_rounds` |
| P1：旧授权和限制继续生效 | 旧任务可沿用已撤销授权；请求 rounds 被忽略 | 每次启动/续派检查当前授权和限制；明确拒绝未知限制，保留请求的更低轮数 | `test_queued_task_rechecks_permission_before_spawning`、`test_revoked_write_permission_applies_to_legacy_continuation`、`test_requested_round_limit_is_preserved` |
| P1：重启后旧 writer 未确认退出 | 发出 SIGTERM 后立即开放同一工作树续派 | 对可验证归属的旧进程组终止并确认；无法验证归属时维持不可直接续派状态 | `test_crash_recovery_stops_verified_writer_before_making_task_resumable` |
| P1：升级误报与遗漏忙任务 | 只看前 100 个任务、忽略回执后的回合/残留 PID、MCP 刷新失败仍宣布成功 | 分页检查、核对存活 PID、服务端关闭保护、实例 ID 识别替换、MCP 刷新失败返回错误 | `tests/test_upgrade.py` |
| P1：暂存区范围漏检 | 文件恢复后，暂存区越界修改可能未报告 | 同时检查暂存区与工作目录；HEAD 必须包含任务原基线 | `test_staged_outside_scope_edit_is_reported_even_when_working_file_is_restored` |
| P2：后台循环异常后静默退出 | RPC 存活但调度或通知停止 | 后台循环有监督、doctor 暴露服务健康，异常通知后恢复程序监督 | `test_service_failure_is_visible_then_supervision_recovers` |
| P2：旧程序打开新版库 | 无条件将 user_version 写回 4 | 拒绝打开更高版本的数据库 | `test_older_executor_does_not_downgrade_newer_database` |

代码主要位于 `src/codex1cc/{common,daemon,store,handoff_host,workspace,upgrade}.py`。

## 产品边界与尚未关闭的风险

1. **当前交付单位是有上限的一个目标。** 轮数与 Codex 接管次数最高 10；没有通用的跨任务依赖图、自动集成基线、发布阶段控制器。现有写任务提示还明确禁止推送、合并和部署。因此不能将“单目标自动修复闭环”宣传为任意大型计划从开发到发布全自动。已有用户发布授权应由未来专门的发布流程保存和执行，不能简单删掉提示中的限制。
2. **长程可靠性证据仍不足。** 历史有小型真实双模型闭环；本次故障注入通过并不替代真实模型、长日志、宿主重启、网络恢复的长程验收。本次未重新运行付费真实模型闭环，也未在 macOS 实机复验。
3. **持久日志容量需要治理。** 现场数据库约 247.8 MiB，`claude_event` 约 71.5 万条；原生写任务保留策略不会自动裁剪这些历史。应将大量原始输出移入有保留策略的日志文件，数据库保存状态和证据索引，并增加磁盘低水位检查。不得直接删除已有证据以掩盖问题。
4. **部分 Git / CLI 检查仍同步运行在事件循环。** 每条有超时，但慢文件系统或挂起的 Git 可能拖慢其他 RPC。下一步应在明确的任务锁/项目锁下将阻塞 I/O 移出事件循环，并加入延迟门禁；不能仅套一层线程而引入并发派发竞态。
5. **验收内容仍由 Codex 判断。** 服务能检查条目数量、HEAD 与范围，无法证明模型填写的测试结论是真的；未提交改动也不能仅凭 HEAD 唯一标识。重要发布需要机器可验证的命令退出码与产物摘要，以及审查时的完整工作树指纹。
6. **底层写任务是可信项目执行。** 独立工作树不会限制任意 Bash 的文件或网络访问；进程组管理也不能追踪任意主动脱离会话的守护进程。这是已有产品边界，需要在授权和任务约束中明确。

## 本次验证结果

- 基线：56 项测试通过；第一组新增故障注入在原实现上 6 项全部失败。
- 修复后 Linux / WSL：Python 3.10 完整 81 项通过（37.985 秒）；Python 3.12 完整 81 项通过（37.933 秒）。命令：`PYTHONPATH=src python -m unittest discover -s tests -v`。
- 随后补充并发冷启动竞态测试：在 `55e66ed` 的隔离副本上复现第二执行器未被拒绝、测试超时；修复版在 Python 3.10 / 3.12 均通过。合计验证 82 项；这项测试专门将两个启动请求放进“尚未绑定 socket”的窗口。
- `uv build` 生成 sdist 和 wheel 成功；新虚拟环境安装 wheel 后运行 `python -I tests/smoke_installed.py` 通过，实际检查 daemon / doctor / MCP initialize / list_tools / list_tasks / shutdown。
- 全新依赖环境出现上游 `pydantic-settings` 的 `lifespan` 前向引用警告，本次握手与工具调用仍成功。应在依赖兼容性门禁中跟踪，不能将本机已有依赖环境视为全部安装环境。
- `git diff --check` 通过；没有运行真实模型新回合，没有替换正在运行的生产执行器。

## 后续发布门禁

每次发布必须满足：

- Linux Python 3.10 / 3.12 完整回归与已安装 wheel 的 MCP 冒烟通过；macOS 使用现有 CI 矩阵验证，Linux 专属 `/proc` 计时测试明确跳过。
- 新缺陷先增加能失败的回归，不能只凭一次正常模型任务通过发布。
- 真实 Codex ↔ CC 验收至少包含失败修复、提问回答、验收完成，以及接管期间执行器/宿主重启后的效果核对；不允许重放不确定副作用。
- 长程验收覆盖连续多轮、日志量、断网恢复、取消和升级；记录 RPC 响应延迟、进程/文件描述符数量、数据库增长与无人处理事件数。
- 发布时声明准确范围：未关闭的产品能力和验证项保持公开，不把基础设施阻塞标成业务目标完成。

CI 已增加单 job 超时和全新 wheel 安装冒烟，避免源码测试通过而安装后失败。任务分支、生产发布和稳定版标签不属于此次审计的交付物。
