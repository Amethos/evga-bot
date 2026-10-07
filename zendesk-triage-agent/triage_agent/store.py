"""Small JSON files on disk: audit log, approval queue, and per-ticket state."""

import datetime
import json
import uuid
from pathlib import Path


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Audit:
    """Append-only record of every decision and action (data/audit.jsonl)."""

    def __init__(self, path: Path):
        self.path = path

    def log(self, kind: str, **fields) -> None:
        record = {"ts": now(), "kind": kind, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        summary = fields.get("summary") or fields.get("status") or ""
        ticket = f" #{fields['ticket']}" if "ticket" in fields else ""
        dry = " (dry run)" if fields.get("dry_run") else ""
        print(f"[{record['ts']}] {kind}{ticket}{dry} {summary}".rstrip())


class JsonFile:
    def __init__(self, path: Path, default):
        self.path = path
        self.default = default

    def load(self):
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)


class Queue:
    """Things waiting on you: permission changes and proposed rules.

    Status flow: pending -> approved -> done | failed, or pending -> rejected.
    The background agent executes approved items; `review` only records your decision.
    """

    def __init__(self, path: Path):
        self.file = JsonFile(path, [])

    def all(self) -> list[dict]:
        return self.file.load()

    def add(self, kind: str, **fields) -> dict:
        items = self.all()
        item = {"id": uuid.uuid4().hex[:6], "kind": kind, "status": "pending", "created": now(), **fields}
        items.append(item)
        self.file.save(items)
        return item

    def update(self, item_id: str, **fields) -> dict:
        items = self.all()
        for item in items:
            if item["id"] == item_id:
                item.update(fields)
                self.file.save(items)
                return item
        raise KeyError(item_id)

    def with_status(self, status: str, kind: str | None = None) -> list[dict]:
        return [i for i in self.all() if i["status"] == status and (kind is None or i["kind"] == kind)]

    def open_for_ticket(self, ticket_id: int) -> list[dict]:
        """Permission changes for this ticket that are still waiting on you or on the agent."""
        return [i for i in self.all() if i["kind"] == "permission" and i.get("ticket") == ticket_id
                and i["status"] in ("pending", "approved")]
