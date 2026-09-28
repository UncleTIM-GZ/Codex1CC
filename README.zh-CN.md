# Codex1CC 使用说明

[English README](README.md) · [产品需求文档](Codex1CC%20产品需求文档.md) · [验证记录](docs/validation.md)

Codex1CC 让你在 Codex 中把一项明确的工作交给 Claude Code（简称 CC），稍后再让 Codex 检查结果。你用自然语言下指令；CC 只读取你授权的项目文件并给出结论。即使关闭 Codex 对话，任务记录仍保存在运行工具的电脑上，重新打开 Codex 后可以继续查看。

这个工具需要安装在能够访问项目文件和 Claude Code 的电脑上。安装后，Codex 通过 MCP（连接外部工具的接口）调用它；你无需自行运行网页服务。工具不会主动提醒你任务已完成，也不会唤醒已关闭的对话。

**当前是 alpha 只读版。** Linux + bubblewrap 已通过开发环境的真实任务测试；WSL 走 Linux 路径。macOS 的真实任务隔离尚未实现，原生 Windows 未支持。CC 不能修改项目、运行项目命令或执行发布。完整 v1 发布门槛见 [验证记录](docs/validation.md)。

## 1. 安装前准备

- Python 3.10+；推荐使用较新的 `uv` 或 `pipx` 安装隔离的 Python 工具环境。
- 支持 stdio MCP 的 Codex 客户端，以及已能登录并调用模型的 Claude Code CLI。
- Linux/WSL 上安装 `bubblewrap`（命令名 `bwrap`），并允许非特权用户命名空间。
- 目标项目的文件位于运行工具的电脑上，且你知道希望允许 CC 读取哪些文件。模型调用会产生服务商费用。

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

应看到 `codex1cc` 已启用。若 Codex 会话在注册之前已打开，重启或刷新客户端。`doctor` 会按需启动后台程序，只检查基础运行条件；它不验证模型账号、服务端模型 ID 或目标项目的任务结果。Codex CLI 的项目目录和 MCP 配置方式见 [官方 OpenAI 文档：CLI](https://learn.chatgpt.com/docs/codex/cli)及 [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

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

下面的操作示例使用 `demo` 项目；克隆 Codex1CC 仓库**不会自动授权任何项目**。首次配置后，可用 `codex1cc doctor` 检查运行条件；首次真实任务还需检查模型认证是否有效。

## 4. 每次进入项目怎么开始

在项目目录中启动 Codex；桌面或 IDE 客户端则打开该项目工作区。告诉 Codex 你要管理哪个已授权项目，例如上文配置的 `demo`。打开目录本身不会自动选定 Codex1CC 项目。

```bash
cd /absolute/path/to/demo
codex
```

如果已授权 `demo`，新会话可以直接说：

> 继续 demo 项目。先看看当前分支和未提交的改动，再查看之前交给 CC 的任务进展。如果有问题需要我回答，或者有结果可以验收，请告诉我；不要重复派任务，也不用一直等它完成。

Codex 会自行查询任务状态；你不需要记住工具名称或状态代码。任务记录不依赖 Codex 聊天记录，新会话仍能查到原目标、验收标准和结果。已完成、失败或取消的任务内容在 30 天后、后台程序再次启动时清理；待回答和待验收的内容不会按这条规则清理。

## 5. 怎样给 CC 派任务

用自然语言说清楚**想解决什么、要看哪些资料、怎样才算完成、希望得到什么结论**。有时间或费用要求，也一并告诉 Codex。Codex 负责核对授权范围并填写工具参数；你不需要写 JSON 或记住函数名。

以下指令基于上文的 `demo` 配置；使用时换成你的项目名和资料范围：

> 请让 CC 对照 demo 项目的 README、`docs` 和相关 `src` 文件，找出文档中没有代码依据或仍需核实的说法。每项附上依据和文件路径，区分已确认事实与待核实问题。只做分析，不改文件，也不要声称跑过测试。交付简短结论和建议的第一项工作；派发后告诉我任务编号，下次我回来再验收。

把几个相互依赖的小步骤写进同一个任务。只有互不依赖、能真正并行的工作才考虑分别交付。每个项目同一时刻最多一个 Codex1CC 活跃任务；跨项目任务可并行。任务完成后，CC 只返回结论、证据和阻塞项，Codex 负责核查。

## 6. 稍后回来：回答、验收、继续或取消

可以关闭 Codex 对话。下次从项目目录打开新对话，先按第 4 节查询。若手上只有任务编号，可说：

> 帮我看看 CC 上次的任务，编号是“这里填任务编号”。告诉我进展、需要回答的问题和结果；不要重新派发。

| 看到的情况 | 你可以怎么做 |
|---|---|
| CC 还在等待或工作 | 记下任务编号，以后再问 Codex；无需持续查看。 |
| CC 提了一个问题 | 让 Codex 说明问题，你回答后由 Codex 转给 CC。 |
| CC 已交结果 | 让 Codex 对照原要求验收；证据不足时可要求补查。 |
| 任务失败或中断 | 先了解原因，修复后再明确交付新任务；系统不会自行重试。 |
| 已完成或已取消 | 无需处理；记录在保留期内仍可查询。 |

验收指令示例：

> 帮我验收 demo 项目上次交给 CC 的任务。对照原要求检查结论和证据；证据不足就告诉我缺什么，条件允许时再让 CC 补查。合格后登记完成，并告诉我最终结论。不要把 CC 自称“测试通过”当成真的跑过测试。

不再需要的任务，可以告诉 Codex 取消并给出任务编号。验收登记不会改动或发布项目。补查使用**原来的文件快照**；项目文件变化后，需要新交一项任务才能看到新内容。

## 7. 给 Codex 的执行约定（AI 可直接读取）

1. 先确认用户说的项目对应已配置的 `project_id`；不要只凭当前目录猜测 ID。进入新会话时按需调用一次 `list_tasks(project_id=..., statuses=["queued","running","continuing","waiting_answer","review_required","failed","interrupted"])`，只对待处理项调用 `get_task`。
2. 对一项完整、边界清楚的目标只调用一次 `submit_task`。把目标、背景、验收标准、交付物、配置中允许的 `scope`、预算和提问规则一次传清；`request_id` 唯一且重试复用。
3. 提交后给用户任务 ID 和已接受状态，即结束本轮。不按定时器调用 `list_tasks`/`get_task`，不占着对话等 CC 完成。
4. 下次自然交互时检查 `waiting_answer` 或 `review_required`。回答用 `respond_task`；验收先看原要求与结果，再决定 `continue_task` 或 `complete_task`。`failed`/`interrupted` 不自动重试。
5. 只把可复用的公共事实写入项目共享文件；任务输出只交简短结论和证据。只在可独立并行的情况下拆任务。当前 CC 不能编辑或运行测试。

## 8. 限制、费用和隐私

Linux 使用 bubblewrap 隔离 CC 的只读快照；缺少隔离环境时任务会失败。CC 的可用工具限于读取、搜索和内部提问，不能运行项目命令、浏览网页或编辑文件。此 alpha 尚未完成跨机器和 macOS 验证，**不要把它作为敏感项目的充分安全边界**。

任务秒数、轮数与费用有项目上限。Claude CLI 报告的 USD 费用用于限额判断，可能与服务商最终账单不同；缺失费用报告时不允许继续下一轮。CC 使用你自己的 Claude Code 认证与模型服务。任务数据、事件和快照保存在运行工具的电脑上，可通过 `codex1cc doctor` 查看路径。

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

卸载工具不会自动删除项目共享文件、项目配置或已保存的任务历史。删除这些文件是独立且不可逆的操作。

从源码开发或验证：

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

自动化测试使用假的 Claude CLI，不会产生模型调用；真实任务与各平台的验证范围见 [验证记录](docs/validation.md)。
