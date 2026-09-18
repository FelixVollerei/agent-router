# Engineering Model Router

**Cheap where safe. Expensive where necessary. Escalate early when evidence says so.**

这是一个本地需求研究与模型调度工作台，也提供 Python 3.11+ CLI/库。默认从一句需求开始，经过两轮预研生成可审阅的任务简报，再交给选定模型执行。无第三方运行依赖。

项目内置轻量 DeepSeek Harness，也支持通过本机 Codex CLI 执行任务；不依赖某个独立 Harness 软件的私有接口。

## 下载与启动

当前源码版本为 **v0.4.1**，对应[方案 v1 定版](docs/V1.md)。桌面工作台主要面向 Windows，要求 Python 3.11 或更新版本。新增宿主工程执行接口首版仅支持 Windows。源码运行没有第三方 Python 依赖；版本号不代表已经发布 Release。

下载仓库源码或 Release 中的 ZIP，解压后在项目目录运行：

```powershell
python -B -m model_router.desktop
```

页面打开后，在左下角「设置」填写自己的 DeepSeek API Key。需要使用 Codex 执行时，请先安装并登录本机 Codex CLI。没有模型连接也可以生成基础模板。

要在 Windows 桌面创建快捷方式：

```powershell
python -B scripts/install_desktop.py
```

快捷方式会根据当前电脑的 Python 与项目目录生成，因此源码包不附带其他电脑的快捷方式。

## 桌面页面（推荐）

双击桌面的 **Model Router**，即可在浏览器中打开本地中文操作台，不需要命令行。

1. 左下角「设置」连接 DeepSeek，可勾选 Windows 账户加密保存。Codex 使用本机已登录的账户。
2. 在「需求发布（简易）」输入需求。无需工程路径、文件类型或权限表单；可主动添加文本资料。「高级」提供可选的工程、范围、检查与任务特征。
3. 等待两轮预研，审阅模型、Prompt、文件计划、验收与权限/插件建议。可继续聊天修改，也可直接编辑方案，再点「发布此方案」。
4. 在会话内查看发布记录、执行消息、检查报告和成果目录。新 Codex 运行保留会话，可恢复到具体 CLI 会话；DeepSeek 运行历史在本工作台查看。

左侧保存会话与项目标签，并保留旧版运行记录。方案按版本保存，发布固定使用审阅版本、模型和 Prompt，重复发布同一版本不会重复运行；后续调整可生成新版本。关闭页面不会停止服务；「设置 → 退出本地服务」结束服务。再次双击图标会复用已运行的新版服务。

预研使用需求、对话、附件与主动指定工程中的最多8份文件。可展开「公开资料预研」，显式填写公开检索词或URL；Brave搜索读取 `BRAVE_SEARCH_API_KEY`，未配置时仍可读取指定公开页面。来源、采集时间与失败随方案保存，不自动把会话或工程变成搜索词。插件栏目仍是用途建议。未连接分析模型时提供明确标识的基础模板；无工程的任务在独立目录生成交付物。

页面只监听随机的本机回环端口，使用会话令牌和同源校验；不会发布到互联网。密钥从不返回前端，不进入普通JSON配置；只有主动勾选时才写入 Windows DPAPI 加密文件。页面状态、加密密钥和执行记录在 `%LOCALAPPDATA%\EngineeringModelRouter`。图形界面与原CLI的状态目录分别管理。

快捷方式需要重建时，在本项目目录执行：

```powershell
python -B scripts/install_desktop.py
```

或手动启动页面：`python -B -m model_router.desktop`。详见 [页面使用指南](docs/DESKTOP.md)。

## 已实现

- Task Analyzer：调用廉价模型提取结构化特征，引用来源，校验类型；不采纳 confidence。离线模式保留未知事实。
- Policy：风险/DDL 安全门槛 → 历史失败过滤 → 综合成本选择；五个执行档位和 DeepSeek + Sol Review 组合。
- DeepSeek：真实 Chat Completions 接口 + 内置受控工具 Harness，支持工具调用上下文。
- Codex：正式 `exec --json`，显式模型/推理等级，Windows 原生沙箱，事件监控。
- 强制运行时、探索步数、上下文、失败检查、无进展预算；失败压缩交接与逐级升级。
- 隔离副本、原始状态哈希、每次尝试 checkpoint、diff、独立检查、只读 review。
- SQLite 抽象经验；Codex app-server 实时额度查询，失败时回退手动配置。
- 显式推广经过验证的候选修改；原始项目发生变化则拒绝覆盖。
- 自定义模型/供应商目录、按需发现与经审阅评测的有限排序修正；配置、发现结果和实际可执行性分开。
- `router.jobs/v1` 宿主接口：持久幂等、单执行槽、事件重放、总时限、执行器调用预算、取消与受控候选成果。Python宿主client和完整示例已提供。

## 快速开始

在项目根目录运行，不必安装依赖：

```powershell
python -B -m model_router --config router.example.toml doctor
python -B -m model_router --config router.example.toml plan --task examples/batch-task.json --prompt
python -B -m model_router --config router.example.toml plan --task examples/scada-task.json
python -B -m unittest discover -s tests -v
```

也可 `python -m pip install -e .`，然后使用 `model-router` 命令。

CLI 的 API Key 通过 `DEEPSEEK_API_KEY` 环境变量传入；桌面工作台也支持 Windows 账户加密保存。仓库与发布包不包含密钥、个人会话或执行记录。PowerShell 可避免在命令历史中写入明文：

```powershell
$routerSecret = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $routerSecret).Password
python -B -m model_router --config router.example.toml run --task examples/batch-task.json --repo examples/demo_repo
Remove-Item Env:DEEPSEEK_API_KEY
```

`run` 输出候选目录和验证状态，不自动修改原工程。查看其中的 `changes.diff`、`verification-*.json`、`result.json`。确认候选后，显式应用：

```powershell
python -B -m model_router promote 'C:\absolute\path\to\runs\RUN_ID'
```

`state_dir` 必须在目标工程之外；示例配置指向本项目的同级 `Agent Router Runs` 目录。默认不创建 Git，也不转换 SVN。

## 从自然语言开始

```powershell
python -B -m model_router --config router.example.toml analyze '按照规范批量修改100个变量，必须保留原值' --repo examples/demo_repo --out task.json
```

检查生成的 profile，并补全执行授权边界 `allowed_files`、`forbidden_files`、主机命令名称 `checks` 和验收标准。Analyzer 不替用户发明任意 shell 命令或授权文件范围。然后 `plan --task task.json` / `run --task task.json --repo ...`。

`--offline` 可不联网生成保守的初始 profile；它不会靠关键词假装完成可靠分析。`deadline_at` 使用带时区的 ISO 日期，例如 `2026-09-10T16:00:00+07:00`，每轮按实际剩余时间计算。`deadline_urgency` 仅用于没有绝对 DDL 时的档位近似。

## 用户覆盖

```powershell
python -B -m model_router --config router.example.toml plan --task examples/scada-task.json --override '/deadline /sol /no-escalation'
```

| 指令 | 行为 |
|---|---|
| `/cheap` | 提高稀缺额度机会成本；保留风险最低档位 |
| `/balanced` | 默认综合成本策略 |
| `/deadline` | 优先时间；收紧预算 |
| `/critical` | critical 门槛，允许最高级升级 |
| `/deepseek` | 强制该执行器，不自动切换；预算和验证仍生效 |
| `/sol` | 至少 Sol Medium |
| `/astra` | 至少 Astra |
| `/ultra` | 显式启用最高档位 |
| `/no-escalation` | 首次尝试失败即停止 |

只解析请求开头的指令，正文或引用中的 `/astra` 不会改变路由。

## 演示与验证

```powershell
# 明确标记为 fixture 的确定性演示：失败 → 交接 → 升级 → 独立检查通过
python -B -m examples.offline_demo

# 可选真实模型实测，会使用对应账户资源
python -B -m examples.smoke deepseek
python -B -m examples.smoke codex

# 只读查询额度，不兑换额度重置
python -B -m model_router usage
```

详细交付说明见 [架构、策略与扩展](docs/ARCHITECTURE.md)、[完整示例](docs/EXAMPLE.md)、[测试结果与限制](docs/VALIDATION.md)。

## 后续方向

本版实现了原计划的第一条可用路径，单工程真实模型与墨小汐PC往返已有证据，权威评测校准、复杂工程和完整多Agent能力继续分层验收。参见[路线图](docs/ROADMAP.md)与[测试结果](docs/VALIDATION.md)。

## 接入墨小汐等宿主

先编辑受信配置中的workspace及读写/检查/模型允许范围。请求只引用workspace ID，不接收远端任意路径或命令。借鉴DSH Standard的连接和领域分层，当前协议仍为Router自有协议，不宣称DSH兼容。

```powershell
# 只读：描述协议能力，不调用模型
python -B -m examples.host_client
# 只读模型发现与公开页面获取
python -B -m model_router catalog --discover codex
python -B -m model_router research --url https://example.com
# 宿主通过私有stdin/stdout调用；配置示例仅授权演示工程
python -B -m model_router --config examples/host-v1.toml serve --client-id moxiaoxi
```

`examples/host-v1-request.json` 是提交合同，`model_router/job_client.py` 提供同步Python client。`python -B -m examples.host_client --execute` 才执行真实演示，会使用模型账户资源。宿主结果始终 `applied=false`；未知执行在重启后隔离，不自动重派。本版没有原地恢复unknown的用户入口，需人工核对后由后续维护能力恢复，不能直接换状态目录当成可以重试。

预算字段 `max_provider_calls` 计顶层执行器调用（包括review），不是执行器内部API请求数或累计token硬限。Codex包外读取隔离依赖其CLI/OS沙箱，文件打包本身不能证明它成立。墨小汐已实现独立工程profile、backend及PC任务入口；旧任务预算保持。接入与真实样本范围见[验证页](docs/VALIDATION.md)。

更新源码后，已有后台服务需在无活动工作时退出并重新启动才能使用新代码；本次源码更新不自动重启用户服务。

## 接口依据

Codex 通过正式的 [非交互执行接口](https://learn.chatgpt.com/docs/non-interactive-mode) 采集事件，通过 [App Server](https://learn.chatgpt.com/docs/app-server) 查询订阅额度。Windows 执行使用 [原生沙箱配置](https://learn.chatgpt.com/docs/windows/windows-sandbox)。DeepSeek 的 [思考与工具调用协议](https://api-docs.deepseek.com/guides/thinking_mode/) 要求工具调用会话保留 `reasoning_content`；适配器保持该字段在进程内传递，不将其作为置信度或工程证据。
