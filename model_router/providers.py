from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

from .models import Event
from .monitor import RunCancelled, digest
from .process import LineProcess, run_command
from .workspace import inventory, permitted, safe_path


class ProviderUnavailable(RuntimeError):
    """Infrastructure failure, not evidence of the model's engineering ability."""


def _http_completion(settings, model, messages, timeout, tools=None, json_mode=False):
    key = os.environ.get(settings.get("api_key_env", "DEEPSEEK_API_KEY"))
    if not key:
        raise ProviderUnavailable(f"Missing environment variable {settings.get('api_key_env', 'DEEPSEEK_API_KEY')}")
    base = settings.get("base_url", "").rstrip("/")
    if not base.startswith("https://") and not base.startswith(("http://localhost:", "http://127.0.0.1:")):
        raise ProviderUnavailable("Provider requires HTTPS (or explicit loopback development endpoint)")
    body = {"model": model, "messages": messages, "max_tokens": settings.get("max_output_tokens", 4096)}
    if "thinking" in settings:
        body["thinking"] = {"type": settings["thinking"]}
    if settings.get("reasoning_effort"):
        body["reasoning_effort"] = settings["reasoning_effort"]
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(.1, timeout)) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ProviderUnavailable("API response exceeded 2MB")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        # Do not dump request headers, keys, or arbitrary provider bodies into logs.
        raise ProviderUnavailable(f"Provider HTTP {e.code}; verify model ID, credentials and endpoint") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ProviderUnavailable(f"Provider transport failure: {type(e).__name__}") from None


def chat_completion(settings, model, messages, timeout, tools=None, json_mode=False, cancel_event=None):
    # A socket timeout alone is insufficient against trickling responses. Supervise a worker
    # process so total wall time is bounded and no background API call survives cancellation.
    child = LineProcess([sys.executable, "-B", "-m", "model_router.api_worker"], Path(__file__).resolve().parent.parent)
    start = time.monotonic()
    payload = dict(settings=settings, model=model, messages=messages, timeout=timeout, tools=tools, json_mode=json_mode)
    child.write_input(json.dumps(payload))
    try:
        while time.monotonic() - start < timeout:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("User requested stop")
            line = child.poll()
            if line and line[0] == "stdout":
                try:
                    data = json.loads(line[1])
                except json.JSONDecodeError:
                    raise ProviderUnavailable("Malformed API worker output") from None
                if "error" in data:
                    raise ProviderUnavailable(data["error"])
                return data["result"]
            if line and line[0] == "overflow":
                raise ProviderUnavailable("API response exceeded supervised event limit")
            if child.finished():
                raise ProviderUnavailable("API worker exited without response")
        raise ProviderUnavailable("API wall-clock timeout")
    finally:
        child.close()


TOOLS = [
    {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}}
    for name, description, props, required in [
        ("list_files", "List task-local file paths", {}, []),
        ("read_file", "Read a UTF-8 workspace file, at most 20000 chars", {"path": {"type": "string"}}, ["path"]),
        ("write_file", "Write an allowed UTF-8 file. No absolute paths, shell, or VCS metadata.",
         {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        ("run_check", "Run one preconfigured acceptance check by name; arbitrary commands unavailable",
         {"name": {"type": "string"}}, ["name"]),
        ("record_evidence", "Record a hypothesis/fact with a concrete citation; this does not reset progress",
         {"category": {"type": "string", "enum": ["known_fact", "hypothesis_tested", "hypothesis_rejected", "question", "next_step"]},
          "text": {"type": "string"}}, ["category", "text"]),
    ]
]


class ManagedChatProvider:
    """Small managed harness: no model-selected shell execution, host observes each tool."""
    def __init__(self, settings, config):
        self.settings, self.config = settings, config

    def preflight(self, model):
        if not os.environ.get(self.settings.get("api_key_env", "DEEPSEEK_API_KEY")):
            raise ProviderUnavailable(f"{model.provider}: API key not configured")

    def run(self, *, task, model, prompt, work, monitor, emit, review=False):
        self.preflight(model)
        messages = [{"role": "system", "content": "You are an engineering executor. Use only supplied tools. Follow scope and budgets. Never claim unrun checks passed. Finish with a concise factual summary."},
                    {"role": "user", "content": prompt}]
        monitor.consume_context(len(prompt.encode("utf-8")))
        offered = [t for t in TOOLS if not review or t["function"]["name"] in {"list_files", "read_file", "record_evidence"}]
        names = {t["function"]["name"] for t in offered}
        tokens = 0
        while True:
            monitor.check()
            response = chat_completion(self.settings, model.model, messages, min(60, monitor.remaining()), offered,
                                       cancel_event=monitor.cancel_event)
            monitor.check()
            msg = response["choices"][0]["message"]
            tokens += response.get("usage", {}).get("total_tokens", 0)
            monitor.consume_context(len(json.dumps(msg, ensure_ascii=False).encode("utf-8")))
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                emit(Event("summary", {"text": msg.get("content") or ""}))
                emit(Event("usage", {"tokens": tokens, "api_cost": None, "codex_usage": None}))
                return
            for call in calls:
                monitor.check()
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"])
                    if name not in names:
                        raise ValueError("Tool not allowed")
                    if name == "list_files":
                        result = list(inventory(work))[:500]
                        emit(Event("tool", {"signature": name, "evidence_hash": digest(json.dumps(result))}))
                    elif name == "read_file":
                        relative = args["path"]
                        path = safe_path(work, relative)
                        if path.stat().st_size > 100_000:
                            raise ValueError("File too large for bounded read; narrow scope")
                        result = path.read_text(encoding="utf-8")[:20000]
                        emit(Event("file_read", {"path": relative, "signature": "read:" + relative, "evidence_hash": digest(result)}))
                    elif name == "write_file":
                        relative = args["path"]
                        if not permitted(relative, task.allowed_files, task.forbidden_files):
                            raise ValueError("File outside allowed scope")
                        content = args["content"]
                        if not isinstance(content, str) or len(content.encode()) > 100_000:
                            raise ValueError("Write exceeds per-file limit")
                        path = safe_path(work, relative)
                        existed = path.exists()
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                        result = "written"
                        emit(Event("file_write", {"path": relative, "change": "modified" if existed else "created",
                                                  "signature": "write:" + relative, "evidence_hash": digest(content)}))
                    elif name == "run_check":
                        check = args["name"]
                        if check not in task.checks or check not in self.config.commands:
                            raise ValueError("Check is not allowlisted for this task")
                        result = run_command(self.config.commands[check], work, min(120, monitor.remaining()), 16000)
                        emit(Event("test", {"name": check, "signature": "check:" + check,
                                           "passed": result["returncode"] == 0 and not result["error"],
                                           "evidence_hash": digest(result["output"]), **result}))
                    else:
                        result = "recorded as executor-reported, not independently established"
                        emit(Event("hypothesis", {"signature": args["category"] + ":" + args["text"],
                                                  "category": args["category"], "text": args["text"][:2000], "provenance": "executor_reported"}))
                except (ValueError, OSError, KeyError, TypeError) as e:
                    result = {"error": str(e)[:500]}
                    emit(Event("tool", {"signature": "rejected:" + name, "error": str(e)[:500]}))
                serialized = json.dumps(result, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": serialized})
                monitor.consume_context(len(serialized.encode("utf-8")))


class CodexCLIProvider:
    def __init__(self, settings, config):
        self.settings, self.config = settings, config

    def preflight(self, model):
        if not shutil.which(self.settings.get("command", "codex")):
            raise ProviderUnavailable("Codex CLI is not installed/on PATH")

    def argv(self, model, work, review=False):
        argv = [self.settings.get("command", "codex"), "exec", "--json",
                "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--color", "never",
                "--sandbox", "read-only" if review else "workspace-write", "-C", str(work),
                "-m", model.model, "-c", "model_reasoning_effort=" + json.dumps(model.reasoning),
                "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=false", "-"]
        if os.name == "nt":
            mode = self.settings.get("windows_sandbox", "elevated")
            if mode not in {"elevated", "unelevated"}:
                raise ProviderUnavailable("Windows sandbox must be elevated or unelevated")
            argv[-1:-1] = ["-c", "windows.sandbox=" + json.dumps(mode)]
        return argv

    def run(self, *, task, model, prompt, work, monitor, emit, review=False):
        self.preflight(model)
        finished_turn, error = False, None
        previous_files = inventory(work)
        monitor.consume_context(len(prompt.encode("utf-8")))
        child = LineProcess(self.argv(model, work, review), work)
        def observe_files():
            nonlocal previous_files
            current = inventory(work)
            for relative in sorted(previous_files.keys() | current.keys()):
                if previous_files.get(relative) != current.get(relative):
                    if review or not permitted(relative, task.allowed_files, task.forbidden_files):
                        raise ValueError(f"Executor changed forbidden file: {relative}")
                    change = "deleted" if relative not in current else "created" if relative not in previous_files else "modified"
                    emit(Event("file_write", {"path": relative, "change": change, "signature": "write:" + relative,
                                              "evidence_hash": current.get(relative, "deleted:" + relative)}))
            previous_files = current
        try:
            child.write_input(prompt)
            while not child.finished():
                monitor.check()
                line = child.poll()
                if not line:
                    continue
                stream, raw = line
                if stream == "overflow":
                    raise ProviderUnavailable("Oversized Codex event")
                if stream == "stderr":
                    emit(Event("diagnostic", {"text": raw[:2000]}))
                    if "blocked by policy" in raw or "writing is blocked by read-only sandbox" in raw:
                        raise ProviderUnavailable("Codex sandbox/policy rejected execution; no model escalation for environment failures")
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    emit(Event("diagnostic", {"text": raw[:2000]}))
                    continue
                kind = data.get("type")
                if kind == "thread.started":
                    emit(Event("session", {"thread_id": data.get("thread_id")}))
                elif kind == "turn.completed":
                    finished_turn = True
                    usage = data.get("usage", {})
                    emit(Event("usage", {"tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                                         "api_cost": None, "codex_usage": None}))
                elif kind in {"turn.failed", "error"}:
                    error = str(data.get("error", data.get("message", "Codex failure")))[:1000]
                elif kind == "item.completed":
                    item = data.get("item", {})
                    typ = item.get("type")
                    if typ == "agent_message":
                        emit(Event("summary", {"text": item.get("text", "")[:12000]}))
                    elif typ == "command_execution":
                        output = item.get("aggregated_output", "")
                        emit(Event("tool", {"signature": item.get("command", "command"),
                                            "command": item.get("command"), "returncode": item.get("exit_code"),
                                            "output": output[-8000:], "evidence_hash": digest(output) if output else None}))
                        observe_files()
                    elif typ == "file_change":
                        observe_files()
                    elif typ not in {"reasoning", "todo_list"}:
                        emit(Event("tool", {"signature": typ or "unknown", "item": str(item)[:2000]}))
            if child.proc.returncode != 0 or not finished_turn or error:
                raise ProviderUnavailable(error or f"Codex exited {child.proc.returncode} without turn.completed")
        finally:
            child.close()


PROVIDERS = {"managed_chat": ManagedChatProvider, "codex_cli": CodexCLIProvider}


def get_provider(config, model):
    settings = config.providers[model.provider]
    kind = settings.get("kind")
    if kind not in PROVIDERS:
        raise ProviderUnavailable(f"Provider kind {kind!r} has no installed adapter")
    return PROVIDERS[kind](settings, config)
