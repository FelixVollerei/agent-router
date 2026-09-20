# router.jobs/v2 — v0.5.0 证据（AgentRouter 侧）

测量时间 2026-09-21（Asia/Bangkok）。仓库：`D:\Codes\Python_Codes\AgentRouter`，分支 `agent/deepseek/router-jobs-v2`，基线 `77d6736`（v0.4.1 快照）。解释器：`D:\Codes\Python_Codes\MoXiaoxi\.venv\Scripts\python.exe -X utf8`（该仓 `dependencies = []`，纯标准库）。

## 结果

| 文件 | 命令 | 结果 |
| --- | --- | --- |
| `identity.txt` | `git rev-parse HEAD`、协议与实现版本 | 本次实现的提交、`router.jobs/v2`（保留 v1）、实现版本 0.5.0 |
| `peer-suite-v2.txt` | `pytest tests -q` | **85 passed / 0 failed**（基线 73 + 本次新增 12；`tests/test_jobs.py` 一个字节未改） |
| `mixed-version-v041.txt` | 混合版本驱动，`PYTHONPATH` = 临时只读克隆 `77d6736` | v1 握手通过且**无** `protocol_versions`；v2/v9 握手 → `incompatible_protocol`；10 键提交 → `submit_fields_must_match_v1`；8 键提交 → 正常接纳（`duplicate:false`） |
| `mixed-version-v2.txt` | 同一驱动，`PYTHONPATH` = 本分支 | v1 握手通过且 `protocol_versions = [v2, v1]`；二次 `initialize(v2)` → 连接协议 v2；v9 → `incompatible_protocol`；10 键提交在 v2 连接上被接纳；8 键提交在 v2 连接上 → `submit_fields_must_match_v2` |

## 兼容矩阵实测（本次覆盖的行）

| 调用方 | 对端 | 实测结果 |
| --- | --- | --- |
| v1 调用方 | v0.4.1 | 逐项不变：v1 握手、8 键提交被接纳（`mixed-version-v041.txt` 末尾两条） |
| v1 调用方 | v2（本次实现） | **仍按 v1 服务**：v1 握手通过、`describe()` 附带 `protocol_versions` 供发现，8 键语义不变 |
| 任意调用方 | v0.4.1 | v2 握手被拒（`incompatible_protocol`）——老对端不假装支持 |
| v2 调用方 | v2 | v2 握手、闭合 10 键被接纳、v1 形状被拒 |
| — | 畸形/未知版本（`router.jobs/v9`） | 两个修订都具名拒绝 |

## 未覆盖

- **没有真实子进程 stdio 传输**：驱动调用 `serve()` 并传入真实字节流（该仓自身线协议测试的同一路径），但两侧是同一台机器上的两个 python 进程内的内存管道，不是 `JobClient` 的 `Popen` 管线。真正的 `Popen` 端到端属于 MoXiaoxi 侧集成验证。
- **没有 v2 下的完整作业执行**：驱动只覆盖声明与校验拒绝，因此不启动 worker。v2 下的服务级执行（提交→结算→取消→制品）由 `tests/test_jobs_v2.py` 用该仓的 fixture worker 覆盖。
- **v0.4.1 副本是只读克隆**，未修改原仓；原仓工作树改动数为 0。
- **PROTOCOL-VERSIONING.md §6 要求的"调用方版本 × 对端版本"完整组合**只完成了对端侧可行的一半；MoXiaoxi 侧的阶梯实现完成后需再跑一次。
