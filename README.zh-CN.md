# Codex1CC 使用说明

[English README](README.md) · [产品需求文档](Codex1CC%20产品需求文档.md) · [验证记录](docs/validation.md)

Codex1CC 是本机 MCP 桥接工具：Codex 提交并验收一项完整任务，Claude Code（CC）在后台读取授权文件快照并返回结论。任务保存在本机执行器中；关闭 Codex 对话后，可以在新对话里接续。工具不会按时间轮询 Codex、主动通知你或唤醒已关闭的对话。

**当前是 alpha 只读版。** Linux + bubblewrap 已通过本机真实任务测试；WSL 走 Linux 路径。macOS 的真实任务隔离尚未实现，原生 Windows 未支持。CC 不能修改项目、运行项目命令或执行发布。完整 v1 发布门槛见 [验证记录](docs/validation.md)。

## 1. 安装前准备

- Python 3.10+；推荐使用较新的 `uv` 或 `pipx` 安装隔离的 Python 工具环境。
- 支持本机 stdio MCP 的 Codex 客户端，以及已能登录并调用模型的 Claude Code CLI。
- Linux/WSL 上安装 `bubblewrap`（命令名 `bwrap`），并允许非特权用户命名空间。
- 目标项目已在本机，且你知道希望允许 CC 读取哪些文件。模型调用会产生服务商费用。

## 2. 安装和注册（每台机器一次）

```bash
git clone https://github.com/UncleTIM-GZ/Codex1CC.git
cd Codex1CC
uv tool install .                 # 或 pipx install .
codex1cc init-config             # 首次创建配置；已存在时不要重复运行
codex1cc config-path
codex1cc doctor
command -v codex1cc
```

把最后一条命令输出的**绝对路径**填入以下命令：

```bash
codex mcp add codex1cc -- /absolute/path/to/codex1cc mcp
codex mcp list
```

应看到 `codex1cc` 已启用。若 Codex 会话在注册之前已打开，重启或刷新客户端。`doctor` 会按需启动本机执行器，只检查基础运行条件；它不验证模型账号、服务端模型 ID 或目标项目的任务结果。Codex CLI 的项目目录和 MCP 配置方式见 [官方 OpenAI 文档：CLI](https://learn.chatgpt.com/docs/codex/cli)及 [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## 3. 初始化一个授权项目（每个项目一次）

用 `codex1cc config-path` 找到 JSON 文件，加入项目 ID、根目录、共享上下文、可读路径和预算。例如：

```json
{
  "projects": {
    "demo": {
      "root": "/absolute/path/to/demo",
      "shared_context": "CODEX1CC_CONTEXT.md",
      "read_paths": ["README.md", "docs", "src"],
      "model": "your-working-model-id",
      "limits": {"seconds": 1800, "usd": 0.25, "rounds": 3}
    }
  }
}
```

- 项目 ID 只能由英文字母、数字、`_`、`-` 组成；`root` 必须是已存在的绝对目录。`model` 可省略，让 Claude Code 使用其默认模型；若默认模型在服务商端不可用，可填写已验证的模型 ID。
- `read_paths` 是**最大授权范围**，不是每次任务都要读的范围。每次 `scope` 只能从列表中选**完全相同的条目**。若授权 `docs`，任务可选 `docs`，不能直接改写成 `docs/one.md`；要单独选择文件，就把该文件另行加入授权列表。
- 在项目根目录创建共享上下文文件。可放权威资料入口、稳定约束、可复用的公共事实；不要放密钥、个人资料或未经核实的当前状态。CC 每次都会收到该文件的只读副本，即使它不在 `scope` 中。
- 配置文件仅给当前用户读取，例如 `chmod 600 "$(codex1cc config-path)"`。项目文件会发往你给 Claude Code 配置的模型服务；只授权可以发送的路径。目录中若含符号链接或特殊文件，快照会拒绝；`.git` 不会被复制。
- 项目预算上限由 `limits` 设置。任务可降低秒数和 USD 上限，轮数上限来自项目配置。单次快照最多 **20 MiB / 2000 个文件**；大型资产、构建产物和缓存目录应排除。

共享文件可以从下面的简短模板开始：

```markdown
# Shared project context
- Source of truth: README.md and docs/architecture.md; verify status against current code.
- Important constraint: do not change published interfaces without explicit review.
- Return concise conclusions with evidence paths; record reusable public facts here.
```

本机已配置的 L21 使用 `project_id="L21"`。这是本机配置示例，克隆本仓库到别的机器**不会自动授权 L21**。首次配置后，可用 `codex1cc doctor` 检查运行条件；首次真实任务还需检查模型认证是否有效。

## 4. 每次进入项目怎么开始

在项目目录中启动 Codex；桌面或 IDE 客户端则打开该项目工作区。**当前目录不会自动决定 Codex1CC 的项目 ID**，每次指令应明确说出 ID。

```bash
cd /absolute/path/to/demo
codex
```

如果使用本机 L21，新会话可以直接说：

> 我现在管理 `L21`。请先确认当前分支和工作区状态，再用 Codex1CC 的 `list_tasks(project_id="L21", statuses=["queued","running","continuing","waiting_answer","review_required","failed","interrupted"])` 查看未完成任务。只对需要回答、验收或排查的任务调用 `get_task`。汇报结论和下一步；不要定时轮询，也不要自动重交旧任务。

`list_tasks` 不传 `statuses` 时只列待回答、待验收、失败和中断任务；想看运行中的任务应像上面那样显式列出状态。任务记录不依赖 Codex 聊天记录。新会话也可以直接按任务 ID 调用 `get_task`；返回值包含原目标、验收标准、范围和执行结果。已结束任务的内容在 30 天后、执行器再次启动时清理；待回答和待验收的内容不会按这条规则清理。

## 5. 怎样给 CC 派任务

用自然语言交代**目标、上下文、验收标准、交付物、授权路径、提问规则、时间与费用上限**。Codex 负责把它们填入 `submit_task`，并为每次操作生成唯一 `request_id`；操作重试应沿用原 ID。只读版的 `actions` 必须为 `["read"]`。

以下指令可在已配置的本机 L21 项目直接使用：

> 请用 Codex1CC 的 `submit_task` 给 CC 派发**一项只读任务**，`project_id="L21"`。目标：核对 Production Shell Migration 的文档计划，找出未关闭的阻塞项，并按对下一步决策的影响排序。上下文：先按 `CLAUDE.md` 与 baseline 文档判断资料权威性；只依据本次快照可见的文件，不把旧状态写成现状。`scope=["README.md","CLAUDE.md","docs","openspec","project_chain/docs/baseline"]`。验收标准：每个阻塞项有文件路径和依据；明确区分已确认事实与待验证项；不得声称运行过测试或看过快照外的代码。交付物：简短结论、阻塞项清单、建议的首个独立任务。只在无法继续时用提问工具问我一个具体问题。`actions=["read"]`，时间最多 1800 秒、费用最多 0.25 USD（轮数按项目配置）。只提交一次，告诉我任务 ID 和已接受的范围，然后结束本轮；不要等待或轮询结果。

把几个相互依赖的小步骤写进同一个任务。只有互不依赖、能真正并行的工作才考虑分别交付。每个项目同一时刻最多一个 Codex1CC 活跃任务；跨项目任务可并行。任务完成后，CC 只返回结论、证据和阻塞项，Codex 负责核查。

## 6. 稍后回来：回答、验收、继续或取消

可以关闭 Codex 对话。下次从项目目录打开新对话，先按第 4 节查询。若手上只有任务 ID，可说：

> 请调用 Codex1CC 的 `get_task(task_id="这里填任务ID")`，读出原目标、验收标准、当前状态、结果与待回答问题；不要重新派发任务。

| 状态 | 操作 |
|---|---|
| `queued` / `running` / `continuing` | 记下任务 ID；下次自然交互时再查。无需定时轮询。 |
| `waiting_answer` | 用 `get_task` 查看问题；你给出答案后，让 Codex 调用 `respond_task`。只能在原授权内回答。 |
| `review_required` | 用 `get_task` 对照原验收标准复核结果。合格后调用 `complete_task` 并记录验收说明；需要补查且原快照、预算和轮数足够时，用 `continue_task` 指定补查内容。 |
| `failed` / `interrupted` | 查看失败原因和事件；修复条件后明确提交新任务。中断不会自动重放。 |
| `completed` / `canceled` | 终态；已完成任务仍可按 ID 查询，直至内容过期清理。 |

验收指令示例：

> 请查看 L21 的 Codex1CC 待验收任务，调用 `get_task` 核对原验收标准和证据。若结论缺证据，只在原快照够用且预算允许时用 `continue_task` 补查；符合标准后调用 `complete_task`，向我报告最终结论。不要把 CC 的自述当成已运行的测试。

不再需要的运行中任务可用 `cancel_task` 按任务 ID 取消。`complete_task` 只登记验收，不会改动或发布项目。`continue_task` 使用**原快照**；项目文件变化后，需要新提交任务才能获取新快照。

## 7. 给 Codex 的执行约定（AI 可直接读取）

1. 先确认用户给出的 `project_id` 已配置；不要从当前目录猜测 ID。进入新会话时按需调用一次 `list_tasks`，只对待处理项调用 `get_task`。
2. 对一项完整、边界清楚的目标只调用一次 `submit_task`。把目标、背景、验收标准、交付物、配置中允许的 `scope`、预算和提问规则一次传清；`request_id` 唯一且重试复用。
3. 提交后给用户任务 ID 和已接受状态，即结束本轮。不按定时器调用 `list_tasks`/`get_task`，不占着对话等 CC 完成。
4. 下次自然交互时检查 `waiting_answer` 或 `review_required`。回答用 `respond_task`；验收先看原要求与结果，再决定 `continue_task` 或 `complete_task`。`failed`/`interrupted` 不自动重试。
5. 只把可复用的公共事实写入项目共享文件；任务输出只交简短结论和证据。只在可独立并行的情况下拆任务。当前 CC 不能编辑或运行测试。

## 8. 限制、费用和隐私

Linux 使用 bubblewrap 隔离 CC 的只读快照；缺少隔离环境时任务会失败。CC 的可用工具限于读取、搜索和内部提问，不能运行项目命令、浏览网页或编辑文件。此 alpha 尚未完成跨机器和 macOS 验证，**不要把它作为敏感项目的充分安全边界**。

任务秒数、轮数与费用有项目上限。Claude CLI 报告的 USD 费用用于限额判断，可能与服务商最终账单不同；缺失费用报告时不允许继续下一轮。CC 使用你自己的 Claude Code 认证与模型服务。任务数据、事件和快照保存在本机状态目录，可通过 `codex1cc doctor` 查看路径。

## 9. 常见问题

| 表现 | 检查方式 |
|---|---|
| 找不到 MCP 工具 | `codex mcp list`；检查注册命令的绝对路径，重启或刷新 Codex。 |
| `PROJECT_NOT_ALLOWED` | 核对项目 ID、共享文件和 `scope` 是否逐项等于 `read_paths` 中的条目；检查路径中的符号链接、特殊文件。 |
| `LIMIT_REACHED` | 缩小文件范围，或在项目授权上限内降低任务规模；检查时间、费用、轮数及快照大小。 |
| `SANDBOX_UNAVAILABLE` | Linux 上检查 `bwrap` 和用户命名空间；macOS 的真实任务暂未开放。 |
| `CLI_FAILED` | 运行 `codex1cc doctor`，再检查 Claude Code 的认证、服务端地址及模型 ID。`doctor` 不会替你验证模型调用。 |
| 任务中断或问题过期 | 用 `get_task` 读状态和事件；问题等待超过 24 小时会过期。决定是否重新提交任务。 |

## 10. 停止、卸载与开发验证

先用 `cancel_task` 处理活跃任务，再运行：

```bash
codex1cc stop
codex mcp remove codex1cc
uv tool uninstall codex1cc     # 若用 pipx 安装，则用 pipx uninstall codex1cc
```

卸载工具不会自动删除项目共享文件、项目配置或本机任务历史。删除这些文件是独立且不可逆的操作。

从源码开发或验证：

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

自动化测试使用假的 Claude CLI，不会产生模型调用；真实任务与各平台的验证范围见 [验证记录](docs/validation.md)。
