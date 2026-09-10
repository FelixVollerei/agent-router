import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .handoff import build_prompt
from .models import Task
from .planner import Planner, list_text
from .policy import Policy
from .workspace import safe_path


class ConversationAPI:
    def conversations_list(self):
        with self.lock:
            rows = self.conversations.list()
            linked = {p.get("run_id") for row in rows for p in self.conversations.get(row["id"])["publications"]}
            # Old execution records remain visible without rewriting or inventing conversation turns.
            legacy = [{"id": r["id"], "title": r["request"], "updated": r["time"], "status": r["status"],
                       "project": "旧版任务", "legacy": True} for r in self.history()["runs"] if r["id"] not in linked]
            return {"conversations": sorted(rows + legacy, key=lambda r: r["updated"], reverse=True)}

    def conversation_get(self, identifier):
        with self.lock:
            return self.conversations.get(identifier)

    def message(self, data):
        with self.lock:
            if self.active or self.busy:
                raise ValueError("当前任务仍在进行，请等待或停止后继续")
            request = data.get("message", "")
            if not isinstance(request, str) or not request.strip() or len(request) > 20000:
                raise ValueError("请输入需求（最多20000字）")
            c = self.conversations.get(data["conversation_id"]) if data.get("conversation_id") else self.conversations.create(request[:40])
            repo = data.get("repo", c.get("repo", "")).strip()
            if repo:
                repo = str(self.repo({"repo": repo}))
            attachments = data.get("attachments", c.get("attachments", []))
            if not isinstance(attachments, list) or len(attachments) > 8:
                raise ValueError("最多添加8份文本附件")
            total = 0
            for a in attachments:
                if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not isinstance(a.get("content"), str):
                    raise ValueError("附件必须包含文件名和文本内容")
                safe_path(self.state / "attachment-validation", a["name"])
                total += len(a["content"])
            if total > 50000:
                raise ValueError("附件文本合计最多50000字")
            options = data.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("高级设置应为对象")
            c.update(repo=repo, attachments=attachments, project=str(data.get("project", c["project"]))[:80])
            c["messages"].append({"role": "user", "content": request.strip(), "time": time.time()})
            c.update(status="planning", phase="正在准备预研")
            self.conversations.save(c)
            self.busy = True
            cancelled = threading.Event()
            self.planning_cancel[c["id"]] = cancelled
            cfg = copy.deepcopy(self.config)

        def progress(phase):
            with self.lock:
                current = self.conversations.get(c["id"])
                current["phase"] = phase
                self.conversations.save(current)

        def work():
            try:
                result = Planner(cfg).generate(c, options, progress, cancelled)
                with self.lock:
                    current = self.conversations.get(c["id"])
                    if cancelled.is_set():
                        raise ValueError("预研已停止，可以继续发送需求")
                    result.update(revision=len(current["proposals"]) + 1, created=time.time())
                    current["proposals"].append(result)
                    current["messages"].append({"role": "assistant", "content": result["summary"], "revision": result["revision"], "time": time.time()})
                    if len(current["proposals"]) == 1:
                        current["title"] = result["title"][:80]
                    current.update(status="ready", phase="方案已生成，可继续讨论或审阅发布")
                    self.conversations.save(current)
            except Exception as exc:
                with self.lock:
                    current = self.conversations.get(c["id"])
                    current.update(status="cancelled" if cancelled.is_set() else "error", phase=str(exc)[:1200])
                    current["messages"].append({"role": "assistant", "content": "预研未完成：" + str(exc)[:1200], "time": time.time()})
                    self.conversations.save(current)
            finally:
                with self.lock:
                    self.busy = False
                    self.planning_cancel.pop(c["id"], None)
        threading.Thread(target=work, daemon=True, name="router-research").start()
        return {"conversation_id": c["id"]}

    def save_proposal(self, data):
        with self.lock:
            c = self.conversations.get(data["conversation_id"])
            if c["status"] in {"planning", "running"}:
                raise ValueError("请等待当前任务结束再修改方案")
            if not c["proposals"]:
                raise ValueError("请先生成方案")
            old = c["proposals"][-1]
            if data.get("revision") != old["revision"]:
                raise ValueError("方案已更新，请重新打开会话后编辑")
            edits = data.get("edits", {})
            p = copy.deepcopy(old)
            for key in ("execution_prompt", "recommended_model"):
                if key in edits:
                    if not isinstance(edits[key], str) or not edits[key].strip():
                        raise ValueError("Prompt和模型不能为空")
                    p[key] = edits[key]
            model = self.config.model(p["recommended_model"])
            for key in ("file_plan", "allowed_files", "forbidden_files", "acceptance_criteria", "checks", "plugins", "permissions_notes"):
                if key in edits:
                    p[key] = list_text(edits[key])
            p["allowed_files"] = p["allowed_files"] or ["*"]
            for pattern in p["allowed_files"] + p["forbidden_files"]:
                safe_path(self.state / "pattern-validation", pattern)
            unknown = set(p["checks"]) - self.config.commands.keys()
            if unknown:
                raise ValueError("检查命令未配置：" + ", ".join(unknown))
            task = Task.from_dict(p["task"])
            task.selected_executor = model.id
            task.execution_prompt = p["execution_prompt"]
            task.context = [s for s in task.context if not s.startswith(("审阅的文件计划：", "权限建议（", "插件建议（"))] + ["审阅的文件计划：" + "；".join(p["file_plan"]), "权限建议（不会扩大实际权限）：" + "；".join(p["permissions_notes"]),
                            "插件建议（当前未接入，不能声称调用过）：" + "；".join(p["plugins"])]
            task.no_escalation = True
            for key in ("allowed_files", "forbidden_files", "acceptance_criteria", "checks"):
                setattr(task, key, p[key])
            task.__post_init__()
            p["task"] = asdict(task)
            if p != old:
                p.update(revision=len(c["proposals"]) + 1, created=time.time())
                c["proposals"].append(p)
                self.conversations.save(c)
            route = Policy(self.config).decide(task)
            route.executor, route.next_executor = model.id, None
            route.budget = Policy(self.config).budget(task, model)
            return {"proposal": p, "full_prompt": build_prompt(task, route, commands=self.config.commands)}

    def publish(self, data):
        with self.lock:
            c = self.conversations.get(data["conversation_id"])
            revision = data.get("revision")
            previous = next((p for p in c["publications"] if p["revision"] == revision), None)
            if previous:
                return {"job_id": previous["job_id"], "already_published": True}
            if not c["proposals"] or c["proposals"][-1]["revision"] != revision:
                raise ValueError("请先保存当前审阅方案")
            p = c["proposals"][-1]
            repo = c.get("repo")
            if not repo:
                source = self.state / "sources" / c["id"]
                source.mkdir(parents=True, exist_ok=True)
                for a in c["attachments"]:
                    target = safe_path(source, a["name"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(a["content"], encoding="utf-8")
                repo = str(source)
            result = self.start_run({"repo": repo, "task": p["task"], "conversation_id": c["id"], "revision": revision})
            c["publications"].append(dict(revision=revision, job_id=result["job_id"], run_id=None, status="running", time=time.time()))
            c.update(status="running", phase="已发布，正在独立工作目录执行")
            self.conversations.save(c)
            return result

    def sync_publication(self, job):
        if not job.get("conversation_id"):
            return
        c = self.conversations.get(job["conversation_id"])
        for p in c["publications"]:
            if p["job_id"] == job["id"]:
                p.update(run_id=job["run_id"], status=job["status"], result=job["result"], error=job["error"])
        c.update(status=job["status"], phase=job.get("error") or ("执行模型正在处理任务" if job["status"] == "running" else "执行记录已保存"))
        self.conversations.save(c)

    def execution(self, identifier):
        path = self.run_dir(identifier)
        events = []
        if (path / "events.jsonl").exists():
            with (path / "events.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        events.append(json.loads(line))
                        if len(events) > 1000:
                            events.pop(0)
                    except ValueError:
                        continue
        sessions = list(dict.fromkeys(e.get("data", {}).get("thread_id") for e in events if e.get("kind") == "session"))
        sessions = [s for s in sessions if isinstance(s, str) and re.fullmatch(r"[a-f0-9-]{36}", s)]
        outputs = []
        work = path / "work"
        if work.is_dir():
            from .planner import filenames
            outputs = filenames(work)
        return {**self.detail(identifier), "events": events, "sessions": sessions, "outputs": outputs,
                "result": json.loads((path / "result.json").read_text(encoding="utf-8")) if (path / "result.json").exists() else None}

    def open_execution(self, data):
        path = self.run_dir(data["run_id"])
        if data.get("target") == "codex":
            session = data.get("session_id")
            if session not in self.execution(data["run_id"])["sessions"]:
                raise ValueError("此运行没有可恢复的 Codex 会话")
            command = self.config.providers["codex"].get("command", "codex")
            subprocess.Popen([command, "resume", session, "-C", str(path / "work"), "--sandbox", "read-only"],
                             creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
            return {"opened": True}
        if os.name != "nt":
            raise ValueError("当前平台不支持打开本地文件夹")
        os.startfile(str(path / "work"))
        return {"opened": True}
