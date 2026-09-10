# 完整示例：100 个变量改名

## 用户输入与 Profile

输入：把100个变量的 `OLD_` 前缀改成 `NEW_`，保留整数值；DDL超过72小时，参考规则明确，`check.py` 核对键和值，仅允许修改 `variables.json`。

完整可运行输入是 `examples/batch-task.json`，源工程是 `examples/demo_repo/`。

```json
{
  "task_type": "batch_edit",
  "domain": "plant_scada",
  "requirement_clarity": 5,
  "automatic_verifiability": 5,
  "human_verification_cost": 1,
  "failure_blast_radius": 1,
  "tool_complexity": 1,
  "repo_scope": 1,
  "domain_uncertainty": 0,
  "root_cause_uncertainty": 0,
  "reversibility": 5,
  "deadline_urgency": 1,
  "production_blocker": false,
  "existing_reference_implementation": true,
  "known_acceptance_criteria": true
}
```

这是随任务提供的、可检查的特征，不是模型自评 confidence。独立 `analyze` 可从自然语言生成初始 Profile；没有明确的文件范围和主机检查之前，不能擅自启动修改。

## Routing

```text
Routing: deepseek
Reason:
  - 需求明确
  - 存在参考实现
  - 可自动验证
  - 修改可恢复
  - 在风险门槛合格候选中综合成本最低
Budget: 600s; 2 failed tests; 10 no-progress calls; 30 steps; 64KB context growth
Escalation: sol_medium
```

早期没有历史样本时使用显式先验；不会显示一个捏造的“历史成功率94%”。

## 执行 Prompt 摘要

系统通过 `build_prompt()` 生成下列结构，并加入具体命令、预算和交接：

```text
Goal
Rename variable prefixes in variables.json with no value changes

Relevant Context
100 integer entries OLD_000 through OLD_099.
Isolated copy. VCS metadata intentionally excluded.

Exact Scope / Allowed Files
["variables.json"]

Constraints
Preserve all integer values. Do not modify check.py.

Acceptance Criteria
Exactly 100 NEW_000..NEW_099 keys with matching integer values.

Forbidden Changes
check.py; VCS metadata; files outside isolated workspace; external side effects.

Verification Method
demo_check = ["python", "-B", "check.py"]

Execution Budget
max_debug_loops=2, max_failed_tests=2,
max_no_progress_tool_calls=10, max_exploration_steps=30,
max_runtime=600, max_context_growth=64000.

Escalation Conditions
Host stops at any limit or repeated actions without new evidence.
```

`plan --prompt` 展示当前配置下的完整 Prompt，不发起模型调用。

## Verifier

1. 主机在副本中运行 `python -B check.py`。
2. 程序将实际 JSON 与 `{NEW_000:0, ..., NEW_099:99}` 精确比较。
3. 校验只改变 `variables.json`、确实有预期修改，未改变 `check.py` 或创建保留元数据。
4. 通过后保存候选哈希，输出 `verified`；原始工程仍不改变。

如果只有模型声明完成而检查失败，状态为 failed/blocked，不能推广。

## 失败升级演示

`python -B -m examples.offline_demo` 是明确标记的确定性测试 fixture，不调用真实模型：

```text
Attempt 1, deepseek fixture
  只生成前50个变量
  独立检查失败
  checkpoint-1保存原始状态
  handoff-1.md / ESCALATION.md记录检查错误、已观察事实和diff

Attempt 2, sol_medium fixture
  读取已有候选和压缩交接，不重新探索任务
  补全100个变量
  独立检查通过
  status = verified
```

交接保持所有已查明假设/事实和重要失败信息；每节有限长。命令输出的全文在本地事件日志中，不无限重复塞入下一模型的上下文。

如果 `/no-escalation`，第一轮失败即停止；如果最高允许档位已经失败，返回 blocked；如果缺实际SCADA加载检查则返回 needs_human。没有任何路径把缺验证的结果伪装成成功。

## 本次真实执行

2026-09-09 已分别验证：

- DeepSeek V4 Pro：真实 API 的读文件、写文件、执行检查、结果总结闭环，独立 verifier 通过。
- Codex Sol Medium：真实 CLI 在 Windows 原生沙箱中完成相同任务，独立 verifier 通过。
- 原始 `examples/demo_repo/variables.json` 仍是100个 `OLD_` 键，真实候选留在 `.router-dev/live-*/runs/`。

这些是小型接口/闭环 smoke 测试，不代表 Plant SCADA复杂项目成功率。没有对 Astra / Ultra 进行不必要的真实额度消耗测试。
