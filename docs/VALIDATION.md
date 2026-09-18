# 验证与已知边界

## v0.4.1 / 墨小汐PC接入（2026-09-14）

在v0.4.0方案v1上增补只读`job.lookup(client_task_id)`。它以启动client身份隔离，只查询已持久接纳的任务，不提交、不重派、不消耗新预算，解决宿主丢失job.submit响应后没有job ID的问题。旧方法与协议版本保留。墨小汐已实现独立工程profile、父预算/身份持久化、AgentRouterBackend、stdio client、本机授权范围和候选界面；不宣称DSH标准兼容。

Router完整回归**73项通过**，17.891秒，JavaScript语法、Python AST、diff空白和HTML引用检查通过；[原始输出](evidence/v041-20260914-162244/check-0.txt)、[当次命令/源码指纹](evidence/v041-20260914-162244/results.json)。仍以v0.3.0提交为基线、未提交工作树为受测版本。新增测试覆盖父任务查询的client隔离、缺失和无执行副作用；原v0.4.0证据保留在下方。

真实墨小汐→Gate/TaskRuntime→Router→Codex→独立检查→同会话候选往返已通过一个生成工程样本；[双端结果和源码hash](evidence/moxiaoxi-ar005-20260914/results.json)、[检查报告](evidence/moxiaoxi-ar005-20260914/verification-1.json)、[页面观察](evidence/moxiaoxi-ar005-20260914/ui-observation.json)。该实验主对话使用夹具、工程provider为真实Codex；执行目录与日常库隔离。墨小汐完整回归、新增跨端故障用例、样本数值及限制由[墨小汐P7增补](D:/Codes/Python_Codes/SysManager/MoXiaoxi/docs/acceptance/P07.md#ar-005-工程任务接入增补)维护。

源文件先在授权开发副本验证，再逐文件核对v0.4.0原项目hash后回写。原项目同步证据见[回写指纹](evidence/v041-writeback-20260914.json)。本次没有提交/发布或重启原后台服务。

尚未完成：Brave真实搜索、新研究链路的真实两轮LLM预研、工程质量/费用校准、长时负载、Codex严格包外读隔离、完整远端计费/停止证明、DSH标准兼容。墨小汐Android三项体验修复继续延期。一个生成样本不证明任意真实仓库均可用；自动promote仍不在宿主API中。下方v0.4.0和v0.3章节描述各自当时的状态，不覆盖本节最新接入结论。

## v0.4.0 / 方案v1（2026-09-14）

本轮以v0.3.0 / `7c487c8d6e2638d646f806815395410f76b2e1b1`为提交基线，实际改动尚未提交或发布。可复核的当前代码身份见[最终测试源码SHA256与命令](evidence/v04-20260914-150452/results.json)，不是仅用HEAD代表工作树。

已实现：自定义模型/供应商目录与按需发现；显式公开资料抓取/可选Brave搜索；经审阅、同任务/版本/量纲评测的有界排序；桌面入口；`router.jobs/v1`持久接纳、事件、预算、取消、成果与Python client。保留旧审阅发布与执行核心。墨小汐仅接口合同定版，backend/profile尚未接通。

| 验证 | 结果与证据 |
| --- | --- |
| 完整回归 | **72项全部通过**（原53＋新增19），18.468秒；[原始输出](evidence/v04-20260914-150452/check-0.txt)。包括Windows DPAPI、真实隔离worker、检查脚本篡改前置拒绝、review共享预算、总时限及子孙进程停止 |
| 合同与故障 | 拒绝预算/范围/模型扩大、幂等冲突、单拥有者/执行槽、事件有序分页、版本/字段错误、重启unknown隔离、候选成果hash篡改、原工程及非授权文件不被写回，均由上述确定性测试覆盖 |
| 静态检查 | Python AST、JavaScript语法、diff空白检查通过；HTML无重复ID、JS无缺失静态ID；测试期间源码hash未变，见results.json |
| 真实只读Codex目录 | 正式app-server `model/list`读到6个模型及各自reasoning档；[原始响应](evidence/v04-live-20260914/discovery-codex-user.json)。不推断推理成功、工具能力或剩余额度 |
| 真实公开页面读取 | `research --url https://example.com`获得559字节并保存采集时间/hash；[原始响应](evidence/v04-live-20260914/public-read-user.json)。不调用回答模型 |
| 浏览器交互 | 隔离临时状态、无模型密钥、自动额度关闭；确认模型/评测表单可打开保存、公开研究默认关闭且输入可见、无密钥生成可编辑基础模板并停在审阅。测试服务已退出；[观察记录](evidence/v04-live-20260914/ui-observation.json)；未运行发布或真实工程 |

失败和修正保留：初次旧回归在离线沙箱下DPAPI失败（52/53），同一测试在真实Windows用户下通过；第一轮新增测试发现服务锁重复打开读取与fixture后代PID定位问题，修正后通过。初次实机只读探针在沙箱得到PermissionError/Codex进程退出，按权限流程在当前Windows用户下重跑成功，[失败原始响应](evidence/v04-live-20260914/public-read.json)、[Codex失败](evidence/v04-live-20260914/discovery-codex.json)保留。未把环境失败算作通过，也未降低测试标准。较早完整回归记录保留在[evidence目录](evidence/)，最终结论以上方指纹为准。

本版未验：Brave真实账户搜索、兼容`/models`真实账户的新路径、新研究链路的真实LLM两轮预研、新宿主API的真实模型工程执行、墨小汐全链路、质量/费用校准、长时负载及DSH标准兼容。历史真实DeepSeek/Codex结果仍保留下面，不能替代新链路验收。

边界：宿主执行首版仅支持Windows，Job Object无法建立则拒绝；`max_provider_calls`/`provider_calls`只计顶层执行器调用，包含review，不是内部API次数或token硬上限。只读包不证明Codex无法读取包外文件，仍需验证其CLI/OS沙箱。`remote_stopped=null`保留远端停止未知；unknown不自动恢复或重派，维护入口留后续。需求端不提供promote；本次更新源码后没有重启用户既有后台服务。

## v0.3 需求会话工作台（2026-09-09）

- 53 项自动化测试全部通过，包括旧版执行/策略回归和新增会话流程。
- 新增覆盖：仅输入需求、无工程发布、可选附件、两轮预研与按需文件读取、上下文保留、编辑 Prompt/模型/范围传入真实调度器、重复发布幂等、版本冲突、会话持久化、重启恢复、预研失败及取消、HTTP鉴权。
- 真实 DeepSeek 两轮预研：仅提供待办页面需求，不附工作区或文件规则，161.7秒完成，返回5条研究结论、4项文件计划和1535字任务 Prompt。原始方案保存在本机忽略目录 `.router-dev/research-check.json`。
- JavaScript 语法检查通过；62个静态 HTML ID 无重复，63处静态 JS 引用与本地资源路径验证通过。未运行浏览器交互或截图测试。
- 本机最终启动检查：`ui_version=3`，已保存的密钥正常加载，会话历史接口正常，桌面快捷方式存在。
- 当前预研未接入实时网页搜索；插件仅提供用途建议，自动执行未接入插件。新 Codex 会话持久保存，通过 CLI 恢复；旧版临时会话恢复不作保证。
- 页面审阅发布固定使用指定模型。旧 CLI 的证据驱动升级策略保留。

## 路由场景

| 场景 | 验证结果 |
|---|---|
| 明确规则修改100变量，DDL 3天，diff/自动检查 | DeepSeek |
| Plant SCADA加载20s→90s，未知根因，8h生产blocker | Sol High |
| 按SPEC实现个人游戏功能，可测试 | DeepSeek |
| 明确大规模正式工程重构，高影响 | DeepSeek + Sol Review（允许成本变化后 Sol Medium） |
| Sol High已失败、现场等待 | Astra |
| Astra已失败且critical | Astra Ultra |
| Codex剩10%，普通个人游戏 | DeepSeek |
| Codex剩10%，现场critical blocker | 仍使用高能力Codex，不以额度降级 |

## v0.3历史自动化回归

执行 `python -B -m unittest discover -s tests -v`。

当时结果：**53项测试全部通过**，包含8个路由场景，以及本地页面的会话、版本、发布、接口、鉴权、停止、历史、推广和 Windows 密钥加密测试。v0.4当前结果见页首。

覆盖策略场景、Ultra门控、DDL提升、cheap不降安全门槛、强制执行与禁止升级、历史失败排除、基础设施记录隔离、类型校验；有界无输出进程超时、连续失败检查、重复工具/文件振荡；模型假报成功、scope校验、禁止文件、强制review与review篡改；失败后保留候选与交接、原工作区不污染、源/候选漂移拒绝推广、SVN保留、路径穿越拒绝、过期DDL不启动模型；CLI沙箱参数、主机工具拒绝任意shell、额度桶解释、覆盖指令仅开头生效、Analyzer不采纳无引用置信度。

回归测试不需要 API Key、不消耗模型资源，使用临时目录；离线 fixture 的经验库与真实运行分开。

## 实机验证（2026-09-09）

| 项目 | 结果 |
|---|---|
| Python 3.12.7，本机 Codex CLI 0.153.4 | 已确认 |
| Codex app-server JSON-RPC初始化、额度读取 | 真实调用成功；按指定codex桶取窗口最低剩余值 |
| DeepSeek `/models` | 确认 deepseek-v4-pro 可用 |
| DeepSeek LLM Analyzer | 真实调用得到经过校验的任务对象 |
| DeepSeek managed工具执行100变量改名 | 独立 verifier 通过，1次执行尝试 |
| Codex Sol Medium执行100变量改名 | 独立 verifier 通过，1次执行尝试 |
| 确定性失败→交接→升级fixture | 2轮后独立检查通过 |

实测修复了三项实际接口问题：DeepSeek默认thinking导致分析阶段超时、非预期JSON字段形状、忽略用户配置后需要显式设置Windows沙箱。没有以扩大无限重试或关闭沙箱来解决。

## 当前限制

1. 项目内置最小 managed Harness 和 adapter 注入接口，没有连接独立 DeepSeek Harness 应用。已提供本地聊天工作台与异步预研/执行，但目前同一服务仅支持一项活动工作，没有多任务调度队列。
2. Copied workspace是文件隔离，不是任意不可信程序的完整虚拟机。主机配置的检查脚本必须可信；Codex执行依赖官方沙箱。远程部署、设备操作、数据库写入不在MVP自动执行范围。
3. Codex CLI内部测试/debug-loop和读文件事实的粒度不完全可见；主机读取命令事件、检测文件变化、限制总时间/步骤并独立运行检查。精确工具前审批与取消确认更适合后续完整App Server adapter。
4. 默认副本上限100MB/10000文件，不包含依赖缓存/VCS元数据；大工业工程、含链接的repo、SCADA必须实际加载等需要专门workspace/verifier adapter。未提供自动Git worktree或SVN事务。
5. 初始成功率、耗时、review风险衰减、效用权重尚未以真实工程样本校准。历史支持精确环境分组，但不包含近邻检索、置信区间或在线学习。
6. API成本和每任务订阅消耗没有可靠精确归因，保持NULL；账户额度和token数分开记录。开启DeepSeek思考模式时reasoning等级的统计标签仍需与逻辑ModelSpec一起配置，避免混合不同执行设置。
7. `needs_human` 是未验收终态，不能 `promote`。当前没有人工签收CLI或断点恢复CLI；修改验收条件后可启动新run，保留原run证据。
8. `promote` 检测开始时的源/候选漂移，可捕获错误时恢复文件；不是跨进程锁或断电原子事务。本地state目录由用户信任，不抵御恶意篡改状态/文件竞态。
9. 交接按节压缩，信息可能截断，但明确指向完整事件日志。Codex没有返回的假设/读文件列表保持空；不从模糊总结编造事实。

## 下一阶段最值得做的五项

1. 对接实际 DeepSeek Harness 的事件与取消协议；扩展 Codex App Server 驱动，实现精确工具预算、终止确认、会话恢复。
2. 用真实 Plant SCADA/批量资产/Python/game 任务建立分层基准，校准先验、review收益、deadline权重和耗时分位数。
3. 增加 Plant SCADA可加载验证、工业格式/schema检查、外部不可篡改验收oracle，以及人工验收签收。
4. Git worktree和SVN友好增量快照、推广锁/事务日志、大文件支持；保留主机/设备安全边界。
5. 在现有会话工作台上增加配额桶到模型映射、跨run续接与经验可视化；目录/联网预研首条路径已在v0.4实施，剩余验收见页首及[路线图](ROADMAP.md)。
