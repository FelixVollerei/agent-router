from dataclasses import asdict

from .models import Budget, Route


class NoSafeRoute(RuntimeError):
    pass


class Policy:
    def __init__(self, config, experience=None):
        self.config, self.experience = config, experience

    @staticmethod
    def ultra_allowed(task):
        p = task.profile
        return (task.force == "ultra" or p.critical or task.mode == "critical" or
                "astra" in p.failed_executors or
                (p.hours_left() < 1 and p.failure_blast_radius >= 4) or
                (p.high_value and p.production_blocker and p.domain_uncertainty >= 4))

    def budget(self, task, model):
        p = task.profile
        budget = Budget(max_runtime=600 if model.rank < 2 else 900,
                        max_exploration_steps=30 if model.rank < 2 else 45,
                        max_context_growth=64000 if model.rank < 2 else 96000)
        values = asdict(budget)
        for key in ("default", model.id, p.task_type):
            values.update(self.config.budget_overrides.get(key, {}))
        if p.hours_left() < 24 or task.mode in {"deadline", "critical"}:
            values["max_runtime"] = min(values["max_runtime"], 300 if model.rank < 2 else 600)
            values["max_debug_loops"] = 1
        if p.hours_left() != float("inf"):
            values["max_runtime"] = max(1, min(values["max_runtime"], max(0, p.hours_left()) * 3600 * .3))
        return Budget(**values)

    def decide(self, task, minimum_rank=0):
        p, cfg = task.profile, self.config
        h = p.hours_left()
        mechanical = p.task_type in {"mechanical_edit", "batch_edit", "ui_asset", "data_processing"}
        easy = (p.requirement_clarity >= 4 and p.automatic_verifiability >= 4 and
                p.reversibility >= 4 and p.root_cause_uncertainty <= 1)
        risk = (1.5 * p.failure_blast_radius + p.tool_complexity + p.repo_scope +
                1.5 * p.domain_uncertainty + 2 * p.root_cause_uncertainty +
                (5 - p.requirement_clarity) + (5 - p.automatic_verifiability) + (5 - p.reversibility))
        floor, reasons = minimum_rank, []
        def require(rank, reason):
            nonlocal floor
            floor = max(floor, rank)
            reasons.append(reason)
        if p.requirement_clarity >= 4:
            reasons.append("需求明确")
        if p.existing_reference_implementation:
            reasons.append("存在参考实现")
        if p.automatic_verifiability >= 4:
            reasons.append("可自动验证")
        if p.reversibility >= 4:
            reasons.append("修改可恢复")
        if p.task_type in {"debugging", "performance_debugging", "reverse_engineering"} and p.root_cause_uncertainty >= 3:
            require(3, "根因未知，开放式诊断需可靠执行器")
        if p.domain == "plant_scada" and p.domain_uncertainty >= 4 and not mechanical:
            require(3, "工业软件私有行为/领域不确定性高")
        if p.task_type in {"architecture", "research"}:
            require(3, "架构或理论判断需要高推理能力")
        if p.automatic_verifiability <= 1:
            require(3, "缺少可靠自动验收依据")
        if p.repo_scope >= 4 and not easy:
            require(2, "跨模块范围大")
        if risk >= 30:
            require(3, "综合工程风险高")
        if p.production and h < 24 and not (mechanical and easy):
            require(3 if p.root_cause_uncertainty >= 3 else 2, "正式工程 DDL <24h")
        if p.production_blocker and h < 4 and not (mechanical and easy):
            require(3, "生产 blocker 且 DDL <4h")
        if p.critical or task.mode == "critical":
            require(3, "critical：可靠性优先")
        if (p.onsite or h < 1) and p.production_blocker and not easy:
            require(4, "现场/极紧 DDL 的复杂 blocker")
        if task.mode == "deadline" and not (mechanical and easy):
            require(3 if p.root_cause_uncertainty >= 3 else 2, "用户选择 deadline 模式")
        for failed in p.failed_executors:
            matches = [m for m in cfg.models if m.id == failed]
            if matches:
                require(2 if matches[0].rank == 0 else matches[0].rank + 1, f"{failed} 已可信失败")
        if p.previous_attempt_count and not p.failed_executors:
            require(2, "已有失败尝试")
        forced_rank = {"sol": 2, "astra": 4, "ultra": 5}.get(task.force)
        if forced_rank:
            require(forced_rank, f"用户覆盖：{task.force}")
        forced_deepseek = task.force == "deepseek"
        if forced_deepseek:
            floor = 0
            reasons.append("用户强制 DeepSeek，风险门槛被显式覆盖；预算仍生效")

        review = (p.production and easy and
                  (p.failure_blast_radius >= 3 or p.repo_scope >= 4 or p.human_verification_cost >= 4))
        candidates = []
        from .catalog import evaluation_context
        evaluation, ignored_evaluation = evaluation_context(cfg, task)
        def scarcity_for(model):
            if not model.scarce:
                return 1
            remaining = cfg.codex_budget_remaining
            value = 1 if remaining is None or remaining > 70 else 1.5 if remaining >= 40 else 3 if remaining >= 20 else 6
            if p.production_blocker or p.critical or task.mode in {"deadline", "critical"} or (p.production and h < 24):
                value = min(value, 1.5)
            return value * (2 if task.mode == "cheap" else 1)
        for model in sorted(cfg.models, key=lambda m: (m.rank, m.resource_cost)):
            if model.rank < floor or (model.rank >= 5 and not self.ultra_allowed(task)):
                continue
            if forced_deepseek and model.id != "deepseek":
                continue
            reviewer = "sol_medium" if review and model.rank == 0 else None
            strategy = "execute_review" if reviewer else "execute"
            stats = self.experience.stats(p, model, strategy) if self.experience else None
            success = stats["adjusted_success"] if stats else model.prior_success
            reason = None
            if stats and stats["attempts"] >= cfg.history_min_samples and success < (.85 if p.production else .65) and not forced_deepseek:
                reason = "同领域同类型历史成功率低于安全门槛"
            success = max(.05, success - max(0, risk - 12) * .008 / (model.rank + 1))
            if reviewer and not any(m.id == reviewer for m in cfg.models):
                reason = "必需 reviewer 未配置"
            minutes = (stats["avg_runtime"] / 60 if stats and stats["avg_runtime"] else model.expected_minutes)
            scarcity = scarcity_for(model)
            review_cost = cfg.model(reviewer).resource_cost * cfg.review_cost_fraction * scarcity_for(cfg.model(reviewer)) if reviewer and not reason else 0
            review_minutes = cfg.model(reviewer).expected_minutes * cfg.review_cost_fraction if reviewer and not reason else 0
            residual_risk = cfg.review_residual_risk if reviewer else 1
            urgency = 4 if h < 4 else 2 if h < 24 else 1
            expected_cost = (model.resource_cost * scarcity + review_cost + cfg.time_weight * (minutes + review_minutes) * urgency +
                             (1 - success) * (minutes * urgency + p.human_verification_cost * cfg.human_weight +
                                              p.failure_blast_radius * cfg.failure_weight * residual_risk))
            evidence = evaluation.get(model.id, [])
            # At most 5%, after hard eligibility checks; never call this measured savings.
            adjustment = .05 * sum(e["quality"] for e in evidence) / len(evidence) if evidence else 0
            expected_cost *= 1 - adjustment
            candidates.append(dict(executor=model.id, reviewer=reviewer, rank=model.rank,
                                   estimated_success=round(success, 4), expected_cost=round(expected_cost, 4),
                                   history=stats, rejected=reason, evaluation=evidence, evaluation_adjustment=adjustment))
        eligible = [x for x in candidates if not x["rejected"]]
        if not eligible:
            raise NoSafeRoute("没有满足安全门槛的执行器；需新证据、用户介入或显式模型覆盖")
        best = min(eligible, key=lambda x: x["expected_cost"])
        if best["history"] and best["history"]["attempts"]:
            s = best["history"]
            reasons.append(f"同类历史成功 {s['successes']}/{s['attempts']}；小样本平滑后参与成本")
        if cfg.codex_budget_remaining is not None:
            reasons.append(f"Codex 剩余 {cfg.codex_budget_remaining:g}%；额度仅调整机会成本，不降低安全门槛")
        if best["reviewer"]:
            reasons.append("正式工程影响较大，需 Sol 独立只读 review")
        reasons.append("在通过风险门槛的路径中，预期综合成本最低")
        if evaluation:
            reasons.append("已审阅且同版本同量纲的相关评测最多修正5%排序成本；不改变授权/风险门槛")
        if ignored_evaluation:
            reasons.append(f"有{len(ignored_evaluation)}条评测因缺审阅/时效/身份/可比性而未用于排序")
        model = cfg.model(best["executor"])
        higher = [m for m in cfg.models if m.rank > model.rank and (m.rank < 5 or self.ultra_allowed(task))]
        next_id = min(higher, key=lambda m: m.rank).id if higher and not task.no_escalation and not forced_deepseek else None
        return Route(model.id, best["reviewer"], reasons, round(risk, 2), candidates,
                     self.budget(task, model), next_id)
