"""Authenticated loopback-only UI. Reuses the existing router; no cloud dependency."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from urllib.parse import urlsplit, parse_qs
import webbrowser

from .analyzer import Analyzer
from .config import Config
from .credentials import load_secret, save_secret
from .conversations import Conversations
from .conversation_api import ConversationAPI
from .experience import ExperienceStore
from .handoff import build_prompt
from .models import Task, TaskProfile
from .orchestrator import Orchestrator
from .policy import Policy
from .providers import ProviderUnavailable
from .usage import read_usage, remaining_percent
from .workspace import promote

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "web"


class App(ConversationAPI):
    def __init__(self, config_path: Path, state: Path):
        self.config_path, self.state = config_path, state
        state.mkdir(parents=True, exist_ok=True)
        self.config = Config.load(config_path)
        self.config.state_dir = state / "data"
        if not shutil.which(self.config.providers["codex"].get("command", "codex")):
            found = sorted((Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin").glob("*/codex.exe"), key=lambda p: p.stat().st_mtime)
            if found:
                self.config.providers["codex"]["command"] = str(found[-1])
        # Explorer-launched shortcuts may have a different PATH from the developer terminal.
        import sys
        for command in self.config.commands.values():
            if command and command[0] == "python":
                command[0] = sys.executable
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.busy = False
        self.planning_cancel = {}
        self.conversations = Conversations(state / "conversations")
        self.conversations.recover()
        self.token = secrets.token_urlsafe(32)
        self.instance = secrets.token_hex(16)
        self.port = 0
        self.notice = ""
        if (state / "settings.json").exists():
            self.update_settings(json.loads((state / "settings.json").read_text(encoding="utf-8")), persist=False)
        try:
            key = load_secret(state / "deepseek.dpapi")
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key
        except OSError:
            self.notice = "保存的密钥无法解密，请在模型设置中重新填写。"

    def settings(self):
        c = self.config
        return {"quota": c.codex_budget_remaining, "auto_usage": c.auto_usage,
                "max_minutes": c.max_total_runtime / 60, "max_attempts": c.max_attempts,
                "deepseek_model": c.model("deepseek").model, "thinking": c.providers["deepseek"].get("thinking", "disabled"),
                "sol_model": c.model("sol_medium").model, "astra_model": c.model("astra").model,
                "codex_command": c.providers["codex"].get("command", "codex"),
                "commands": c.commands}

    def update_settings(self, data, persist=True):
        with self.lock:
            if self.active or self.busy:
                raise ValueError("任务进行中，请先等待完成或停止，再修改设置")
            c = copy.deepcopy(self.config)
            for name in ("deepseek_model", "sol_model", "astra_model", "codex_command"):
                if name in data and (not isinstance(data[name], str) or not data[name].strip() or len(data[name]) > 500):
                    raise ValueError("模型名称和命令不能为空")
            if "quota" in data:
                c.codex_budget_remaining = None if data["quota"] is None else float(data["quota"])
            if "auto_usage" in data:
                if type(data["auto_usage"]) is not bool:
                    raise ValueError("自动额度设置必须是布尔值")
                c.auto_usage = data["auto_usage"]
            if "max_minutes" in data:
                c.max_total_runtime = float(data["max_minutes"]) * 60
                if c.max_total_runtime > 86400:
                    raise ValueError("单任务预算最多24小时")
            if "max_attempts" in data:
                c.max_attempts = int(data["max_attempts"])
                if c.max_attempts > 10:
                    raise ValueError("最多10次执行尝试")
            if "thinking" in data:
                if data["thinking"] not in {"disabled", "enabled"}:
                    raise ValueError("无效思考模式")
                c.providers["deepseek"]["thinking"] = data["thinking"]
            if "codex_command" in data:
                c.providers["codex"]["command"] = data["codex_command"]
            c.models = [replace(m, model=data.get("deepseek_model" if m.id == "deepseek" else "sol_model" if m.id.startswith("sol_") else "astra_model", m.model)) for m in c.models]
            c.analyzer_model = c.model("deepseek").model
            if "commands" in data:
                if not isinstance(data["commands"], dict):
                    raise ValueError("检查命令必须是JSON对象")
                c.commands = data["commands"]
            c.validate()
            key = data.get("api_key", "")
            if not isinstance(key, str) or len(key) > 1000:
                raise ValueError("无效密钥")
            current_key = key.strip() or os.environ.get("DEEPSEEK_API_KEY", "")
            if data.get("remember_key") is True:
                if not current_key:
                    raise ValueError("请先填写密钥，再启用加密保存")
                save_secret(self.state / "deepseek.dpapi", current_key)
            elif data.get("remember_key") is False:
                (self.state / "deepseek.dpapi").unlink(missing_ok=True)
            if key:
                os.environ["DEEPSEEK_API_KEY"] = key.strip()
            self.config = c
            if persist:
                temp = self.state / "settings.tmp"
                temp.write_text(json.dumps(self.settings(), ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(self.state / "settings.json")
            return {"saved": True, "key_present": bool(os.environ.get("DEEPSEEK_API_KEY")), "key_saved": (self.state / "deepseek.dpapi").exists()}

    def bootstrap(self):
        with self.lock:
            return {"settings": self.settings(), "key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
                    "key_saved": (self.state / "deepseek.dpapi").exists(),
                    "codex_present": bool(shutil.which(self.config.providers["codex"].get("command", "codex"))),
                    "state_dir": str(self.config.state_dir), "notice": self.notice,
                    "active_job": self.active, "busy": self.busy,
                    "default_profile": asdict(TaskProfile()),
                    "models": [asdict(m) for m in self.config.models]}

    @staticmethod
    def repo(data):
        value = data.get("repo")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请填写工程文件夹的完整路径")
        repo = Path(value).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError("工程文件夹不存在，请检查路径")
        return repo

    def prepare_task(self, data):
        task = Task.from_dict(data["task"])
        if not task.checks or not task.acceptance_criteria or task.manual_verification:
            task.profile.automatic_verifiability = min(task.profile.automatic_verifiability, 1)
        unknown = set(task.checks) - self.config.commands.keys()
        if unknown:
            raise ValueError("检查命令未配置：" + ", ".join(unknown))
        return task

    def exclusive(self, fn):
        with self.lock:
            if self.active or self.busy:
                raise ValueError("已有任务进行中，请等待完成后再操作")
            self.busy = True
        try:
            return fn()
        finally:
            with self.lock:
                self.busy = False

    def analyze(self, data):
        def execute():
            repo = self.repo(data)
            task = Analyzer(copy.deepcopy(self.config)).analyze(data["request"], repo, not data.get("offline", False))
            return {"task": asdict(task), "source": "offline" if data.get("offline") else "deepseek"}
        return self.exclusive(execute)

    def plan(self, data):
        self.repo(data)
        with self.lock:
            cfg = copy.deepcopy(self.config)
        task = self.prepare_task(data)
        store = ExperienceStore(cfg.state_dir / "experience.sqlite3")
        try:
            route = Policy(cfg, store).decide(task)
            return {"route": asdict(route), "task": asdict(task), "prompt": build_prompt(task, route, commands=cfg.commands)}
        finally:
            store.close()

    def start_run(self, data):
        with self.lock:
            if self.active or self.busy:
                raise ValueError("已有任务正在进行，请先完成或停止")
            repo = self.repo(data)
            task = self.prepare_task(data)
            if not task.allowed_files:
                raise ValueError("请填写允许修改的文件，例如 src/*.py")
            cfg = copy.deepcopy(self.config)
            route = Policy(cfg).decide(task)
            if cfg.model(task.selected_executor or route.executor).provider == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
                raise ValueError("请先在模型设置中填写 DeepSeek API Key")
            job_id = secrets.token_hex(16)
            cancelled = threading.Event()
            job = {"id": job_id, "status": "running", "started": time.time(), "route": None,
                   "events": [], "result": None, "run_id": None, "error": None,
                   "cancel": cancelled, "request": task.request, "conversation_id": data.get("conversation_id"), "revision": data.get("revision")}
            self.jobs[job_id] = job
            self.active = job_id
            # Keep bounded in-memory terminal jobs. Full outcomes stay on disk.
            for old in list(self.jobs)[:-50]:
                if self.jobs[old]["status"] != "running":
                    del self.jobs[old]
        def update(**values):
            with self.lock:
                job.update(values)
                self.sync_publication(job)
        def on_event(attempt, event):
            with self.lock:
                job["events"].append({"attempt": attempt, **asdict(event)})
                job["events"] = job["events"][-100:]
        def work():
            try:
                result = Orchestrator(cfg, on_route=lambda r: update(route=asdict(r)), on_event=on_event,
                                      on_workspace=lambda p: update(run_id=p.name), cancel_event=cancelled).run(task, repo)
                update(status=result.status, result=asdict(result))
            except Exception as exc:
                update(status="blocked", error=f"{type(exc).__name__}: {exc}")
            finally:
                with self.lock:
                    job["finished"] = time.time()
                    self.sync_publication(job)
                    self.active = None
        threading.Thread(target=work, daemon=True, name="router-run").start()
        return {"job_id": job_id}

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("当前服务没有此任务，请查看历史记录")
            return copy.deepcopy({k: v for k, v in self.jobs[job_id].items() if k != "cancel"})

    def run_dir(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id or ""):
            raise ValueError("无效运行编号")
        base = (self.config.state_dir / "runs").resolve()
        path = (base / run_id).resolve()
        if path.parent != base or not path.is_dir():
            raise ValueError("运行记录不存在")
        return path

    def history(self):
        rows = []
        base = self.config.state_dir / "runs"
        if base.exists():
            for folder in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
                if not re.fullmatch(r"[a-f0-9]{32}", folder.name):
                    continue
                try:
                    path = self.run_dir(folder.name)
                    task = json.loads((path / "task.json").read_text(encoding="utf-8"))
                    result = json.loads((path / "result.json").read_text(encoding="utf-8")) if (path / "result.json").exists() else {"status": "incomplete"}
                    rows.append({"id": folder.name, "request": task["request"][:250], "time": folder.stat().st_mtime,
                                 "promoted": (path / "promoted.json").exists(), **result})
                except (OSError, ValueError):
                    continue
        return {"runs": rows}

    def detail(self, run_id):
        path = self.run_dir(run_id)
        names = [p.name for p in path.iterdir() if p.is_file() and
                 (p.name in {"changes.diff", "ESCALATION.md", "result.json", "task.json"} or re.fullmatch(r"(?:verification|route|review|prompt)-\d+\.(?:json|md)", p.name))]
        return {"id": run_id, "files": sorted(names), "path": str(path), "promoted": (path / "promoted.json").exists()}

    def artifact(self, run_id, name):
        if name not in self.detail(run_id)["files"]:
            raise ValueError("不允许读取该文件")
        path = self.run_dir(run_id) / name
        if path.is_symlink():
            raise ValueError("不允许读取链接文件")
        with path.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read(100001)
        return {"name": name, "content": content[:100000], "truncated": len(content) > 100000}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log authorization material, key input, or task payloads.

        def reply(self, status, value, mime="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def check(self, auth=True):
            origin = f"http://127.0.0.1:{app.port}"
            if self.headers.get("Host") != f"127.0.0.1:{app.port}" or self.headers.get("Origin", origin) != origin:
                raise PermissionError("仅允许本机同源页面访问")
            if auth and not hmac.compare_digest(self.headers.get("X-Router-Token", ""), app.token):
                raise PermissionError("页面连接已过期，请重新双击桌面快捷方式")

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                self.check(auth=url.path.startswith("/api/"))
                if url.path == "/health":
                    return self.reply(200, {"app": "engineering-model-router", "instance": app.instance, "ui_version": 3})
                assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
                if url.path in assets:
                    name, mime = assets[url.path]
                    return self.reply(200, (STATIC / name).read_bytes(), mime)
                if url.path == "/api/bootstrap":
                    return self.reply(200, app.bootstrap())
                if url.path == "/api/example":
                    return self.reply(200, {"repo": str(ROOT / "examples/demo_repo"), "task": json.loads((ROOT / "examples/batch-task.json").read_text(encoding="utf-8"))})
                if url.path.startswith("/api/jobs/"):
                    return self.reply(200, app.job(url.path.rsplit("/", 1)[-1]))
                if url.path == "/api/history":
                    return self.reply(200, app.history())
                if url.path == "/api/conversations":
                    return self.reply(200, app.conversations_list())
                if url.path.startswith("/api/conversations/"):
                    return self.reply(200, app.conversation_get(url.path.rsplit("/", 1)[-1]))
                if url.path.startswith("/api/executions/"):
                    return self.reply(200, app.execution(url.path.rsplit("/", 1)[-1]))
                if url.path == "/api/artifact":
                    query = parse_qs(url.query)
                    return self.reply(200, app.artifact(query.get("run", [""])[0], query.get("name", [""])[0]))
                if url.path.startswith("/api/runs/"):
                    return self.reply(200, app.detail(url.path.rsplit("/", 1)[-1]))
                self.reply(404, {"error": "页面不存在"})
            except PermissionError as exc:
                self.reply(403, {"error": str(exc)})
            except (ValueError, OSError, KeyError) as exc:
                self.reply(400, {"error": str(exc)})

        def do_POST(self):
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请求必须是JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 200000:
                    raise ValueError("请求大小超出限制")
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                # Drain the bounded body before denying a request; otherwise Windows can reset
                # the connection before the client receives its 403. Never parse/use it before auth.
                self.check()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("请求必须是JSON对象")
                path = urlsplit(self.path).path
                if path == "/api/message":
                    return self.reply(202, app.message(data))
                if path == "/api/proposal":
                    return self.reply(200, app.save_proposal(data))
                if path == "/api/publish":
                    return self.reply(202, app.publish(data))
                if path == "/api/open-execution":
                    return self.reply(200, app.open_execution(data))
                if path == "/api/stop-research":
                    with app.lock:
                        cancelled = app.planning_cancel.get(data.get("conversation_id"))
                        if cancelled:
                            cancelled.set()
                    return self.reply(200, {"stopping": True})
                if path == "/api/settings":
                    return self.reply(200, app.update_settings(data))
                if path == "/api/clear-key":
                    with app.lock:
                        if app.active or app.busy:
                            raise ValueError("任务运行期间不能移除密钥")
                        os.environ.pop("DEEPSEEK_API_KEY", None)
                        (app.state / "deepseek.dpapi").unlink(missing_ok=True)
                    return self.reply(200, {"cleared": True})
                if path == "/api/analyze":
                    return self.reply(200, app.analyze(data))
                if path == "/api/plan":
                    return self.reply(200, app.plan(data))
                if path == "/api/run":
                    return self.reply(202, app.start_run(data))
                if path == "/api/cancel":
                    with app.lock:
                        job = app.jobs.get(data.get("job_id"))
                        if not job or job["status"] != "running":
                            raise ValueError("任务已结束")
                        job["cancel"].set()
                    return self.reply(200, {"stopping": True})
                if path == "/api/usage":
                    def quota():
                        result = read_usage(app.config.providers["codex"].get("command", "codex"))
                        percent = remaining_percent(result, app.config.codex_limit_id)
                        with app.lock:
                            if percent is not None:
                                app.config.codex_budget_remaining = percent
                        return {"remaining": percent}
                    return self.reply(200, app.exclusive(quota))
                if path == "/api/promote":
                    return self.reply(200, app.exclusive(lambda: {"files": promote(app.run_dir(data.get("run_id")))}))
                if path == "/api/shutdown":
                    with app.lock:
                        if app.active or app.busy:
                            raise ValueError("请先停止或等待当前任务结束")
                    self.reply(200, {"stopped": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self.reply(404, {"error": "接口不存在"})
            except PermissionError as exc:
                self.reply(403, {"error": str(exc)})
            except (ValueError, OSError, RuntimeError, KeyError, TypeError, StopIteration) as exc:
                self.reply(400, {"error": str(exc)[:1500]})
    return Handler


def make_server(app, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server


def serve(config_path, state, open_browser=False):
    app = App(config_path, state)
    # OS-assigned loopback port avoids collisions; the desktop launcher uses a verified session file.
    server = make_server(app)
    session = state / "session.json"
    session.write_text(json.dumps({"port": app.port, "instance": app.instance, "token": app.token}), encoding="utf-8")
    try:
        session.chmod(0o600)
        url = f"http://127.0.0.1:{app.port}/#{app.token}"
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        try:
            saved = json.loads(session.read_text(encoding="utf-8"))
            if saved.get("instance") == app.instance:
                session.unlink()
        except (OSError, ValueError):
            pass
