"""Durable, versioned conversations. Caller serializes writes with App.lock."""
import json
from pathlib import Path
import re
import secrets
import time


class Conversations:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("无效会话编号")
        return self.root / (identifier + ".json")

    def create(self, title="新需求"):
        item = dict(id=secrets.token_hex(16), title=title[:80], project="", repo="",
                    created=time.time(), updated=time.time(), messages=[], proposals=[], publications=[],
                    status="idle", phase="", attachments=[])
        self.save(item)
        return item

    def get(self, identifier):
        path = self.path(identifier)
        if not path.exists():
            raise ValueError("会话不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, item):
        item["updated"] = time.time()
        target = self.path(item["id"])
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)

    def list(self):
        result = []
        for path in self.root.glob("*.json"):
            try:
                c = self.get(path.stem)
                result.append({k: c[k] for k in ("id", "title", "project", "updated", "status", "phase")})
            except (ValueError, OSError, KeyError):
                continue
        return sorted(result, key=lambda c: c["updated"], reverse=True)

    def recover(self):
        for row in self.list():
            c = self.get(row["id"])
            if c["status"] in {"planning", "running"}:
                c["status"], c["phase"] = "interrupted", "上次服务退出，记录已保留；可以继续讨论或查看执行记录。"
                for publication in c["publications"]:
                    if publication["status"] != "running":
                        continue
                    publication["status"] = "interrupted"
                    run_id = publication.get("run_id")
                    if isinstance(run_id, str) and re.fullmatch(r"[a-f0-9]{32}", run_id):
                        result = self.root.parent / "data" / "runs" / run_id / "result.json"
                        try:
                            outcome = json.loads(result.read_text(encoding="utf-8"))
                            publication.update(status=outcome["status"], result=outcome)
                        except (OSError, ValueError, KeyError):
                            pass
                self.save(c)
