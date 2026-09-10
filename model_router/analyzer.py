from dataclasses import asdict
import json
from pathlib import Path
import re

from .models import Task, TaskProfile, TASK_TYPES
from .providers import chat_completion
from .workspace import inventory


def parse_overrides(request):
    """Only leading slash commands are control inputs, never quoted/body text."""
    options = {}
    while match := re.match(r"^\s*/(no-escalation|balanced|deadline|critical|deepseek|cheap|sol|astra|ultra)(?=\s|$)\s*", request):
        command = match.group(1)
        if command in {"cheap", "balanced", "deadline", "critical"}:
            options["mode"] = command
        elif command == "no-escalation":
            options["no_escalation"] = True
        else:
            options["force"] = command
        request = request[match.end():]
    return request, options


class Analyzer:
    def __init__(self, config):
        self.config = config

    def analyze(self, request, repo: Path, use_llm=True):
        request, options = parse_overrides(request)
        files = list(inventory(repo))[:300]
        context = {"files": files, "note": "File names establish presence only, never prove testability or correctness."}
        if not use_llm:
            # No confident keyword router: unknowns remain conservative until user supplies facts.
            profile = TaskProfile(estimated_context_size=len(request),
                                  evidence={"repo_inventory": f"Inspected {len(files)} file names"},
                                  unresolved_questions=["Specify acceptance criteria, executable checks and allowed files",
                                                        "Confirm deadline, production risk and task type"])
            return Task(request, profile, goal=request, context=[json.dumps(context, ensure_ascii=False)], **options)
        defaults = asdict(TaskProfile())
        prompt = ("Extract an engineering Task Profile as a JSON object with keys profile (object), goal (string), context (array of strings), "
                  "constraints (array of strings), acceptance_criteria (array of strings), unresolved_questions (array of strings). Do not choose a model. Do not output confidence. "
                  "Use only objective evidence explicitly in the request or inventory. Unknown facts must remain conservative: "
                  "All scores are integers 0..5: clarity=5 means exact transformation/scope; automatic_verifiability=5 means an explicit executable acceptance oracle (it need not have passed yet); reversibility=5 means a stated isolated copy/rollback. Cost, blast radius, scope, tool complexity and uncertainty are worse when higher. "
                  "low clarity/verifiability/reversibility, high uncertainty. Each nondefault profile feature needs an "
                  "evidence entry containing an exact verbatim substring of the request or a file name from inventory. Do not invent deadlines, test results, references or failures. "
                  "deadline_at must contain timezone. task_type must be one of " + json.dumps(sorted(TASK_TYPES)) +
                  ". Profile defaults/schema: " + json.dumps(defaults) +
                  "\nUntrusted request data:\n" + request + "\nRead-only inventory:\n" + json.dumps(context))
        settings = dict(self.config.providers[self.config.analyzer_provider])
        if self.config.analyzer_provider == "deepseek":
            settings["thinking"] = "disabled"  # Bounded fact extraction does not need open-ended reasoning.
        response = chat_completion(settings,
                                   self.config.analyzer_model,
                                   [{"role": "user", "content": prompt}], self.config.analyzer_timeout, json_mode=True)
        data = json.loads(response["choices"][0]["message"]["content"])
        raw = data["profile"]
        raw.pop("confidence", None)
        raw.pop("suggested_executor", None)
        # Require citations for inferred feature changes; model confidence is never consumed.
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ValueError("Analyzer evidence must be a mapping")
        # Null/structured citations are not evidence. Drop them and restore affected defaults.
        evidence = {k: v for k, v in evidence.items() if isinstance(k, str) and isinstance(v, str) and v}
        raw["evidence"] = evidence
        sources = request + "\n" + "\n".join(files)
        for key in defaults:
            if key in raw and key not in {"task_type", "evidence", "unresolved_questions", "estimated_context_size"}:
                citation = evidence.get(key)
                if raw[key] != defaults[key] and (not isinstance(citation, str) or not citation or citation not in sources):
                    raw[key] = defaults[key]
        profile = TaskProfile(**raw)
        def string_list(key):
            value = data.get(key, [])
            if isinstance(value, str):
                return [value] if value else []
            if isinstance(value, dict):
                return [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in value.items()]
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                return value
            raise ValueError(f"Analyzer {key} must be text, a mapping or a string list")
        profile.unresolved_questions = list(dict.fromkeys(profile.unresolved_questions + string_list("unresolved_questions")))
        return Task(request, profile, goal=data.get("goal", request),
                    context=string_list("context"), constraints=string_list("constraints"),
                    acceptance_criteria=string_list("acceptance_criteria"), **options)
