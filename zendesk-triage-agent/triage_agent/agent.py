"""The background loop: triage new activity, run approved changes, report back on tickets."""

import time
import traceback

from .command import CommandAgent
from . import fields
from .decide import decide, ticket_context
from .exchange import ExchangeOperator, describe
from .rules import RuleBook
from .store import Audit, JsonFile, Queue
from .zendesk import ZendeskError

TAG_TRIAGED = "agent_triaged"
TAG_PENDING = "agent_pending_approval"
TAG_HUMAN = "agent_needs_human"
TAG_SUGGEST_CLOSE = "agent_suggests_close"
RULE_NOTE_PREFIX = "#rule"
COMMAND_NOTE_PREFIX = "#agent"


class Agent:
    def __init__(self, cfg, zd, exchange_page=None):
        self.cfg = cfg
        self.zd = zd
        self.exchange_page = exchange_page
        self.audit = Audit(cfg.data_dir / "audit.jsonl")
        self.queue = Queue(cfg.data_dir / "queue.json")
        self.state = JsonFile(cfg.data_dir / "state.json", {"seen": {}, "notes_handled": []})
        self.book = RuleBook(cfg.rules_file)

    # --- main loop ---

    def run_forever(self) -> None:
        while True:
            try:
                self.cycle()
            except ZendeskError as e:
                self.audit.log("error", summary=str(e))
                time.sleep(max(self.cfg.poll_seconds, 300))
                continue
            time.sleep(self.cfg.poll_seconds)

    def cycle(self) -> None:
        self.report_rejections()
        self.run_approved()
        state = self.state.load()
        for ticket in self.zd.search_tickets(self.cfg.ticket_query, self.cfg.max_tickets_per_cycle):
            key = str(ticket["id"])
            if state["seen"].get(key) == ticket["updated_at"]:
                continue  # nothing new since we last looked
            try:
                updated_at = self.handle(ticket, state)
            except ZendeskError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad ticket shouldn't stop the queue
                self.audit.log("error", ticket=ticket["id"], summary=repr(e), trace=traceback.format_exc())
                continue
            state["seen"][key] = updated_at or ticket["updated_at"]
            self.state.save(state)

    # --- one ticket ---

    def handle(self, ticket: dict, state: dict) -> str | None:
        tid = ticket["id"]
        comments, users = self.zd.comments(tid)
        if self.follow_notes(ticket, comments, state):
            # You gave instructions on this ticket; they replace triage for this update.
            return self.zd.get_ticket(tid)["updated_at"] if not self.cfg.dry_run else None

        if self.queue.open_for_ticket(tid):
            return None  # waiting on your approval; leave it alone

        groups = self.zd.groups()
        context = ticket_context(ticket, comments, users) + "\n\nFields:\n" + fields.describe(self.zd, ticket)
        decision, model = decide(self.cfg, self.book.as_prompt(), list(groups), context)
        self.audit.log(
            "decision",
            ticket=tid,
            model=model,
            summary=f"{decision.category}, confidence {decision.confidence:.2f}",
            decision=decision.model_dump(),
        )
        return self.apply(ticket, decision, groups)

    def apply(self, ticket: dict, d, groups: dict[str, int]) -> str | None:
        tid = ticket["id"]
        changes: dict = {}
        if d.priority and d.priority != ticket.get("priority"):
            changes["priority"] = d.priority
        if d.group and groups.get(d.group) and groups[d.group] != ticket.get("group_id"):
            changes["group_id"] = groups[d.group]
        add_tags = [*d.add_tags, TAG_TRIAGED]
        remove_tags = list(d.remove_tags)

        note = [f"Agent triage: {d.category} (confidence {d.confidence:.2f})", d.reasoning]
        custom = []
        for fv in d.fields:
            try:
                custom.append(fields.resolve(self.zd, fv.name, fv.value))
            except ValueError as e:
                note.append(f"Couldn't set {fv.name}: {e}")
        if custom:
            changes["custom_fields"] = custom
        if d.applied_rule_ids:
            note.append("Rules applied: " + ", ".join(d.applied_rule_ids))

        if d.permission_requests:
            add_tags.append(TAG_PENDING)
            note.append("Waiting for approval:")
            for req in d.permission_requests:
                req_dict = req.model_dump()
                item = self.queue.add(
                    "permission",
                    ticket=tid,
                    subject=ticket.get("subject"),
                    request=req_dict,
                    reasoning=d.reasoning,
                    dry_run=self.cfg.dry_run,
                )
                note.append(f"  [{item['id']}] {describe(req_dict)}")
        elif d.needs_human:
            add_tags.append(TAG_HUMAN)
            note.append(f"Needs a person: {d.needs_human_reason}")
        elif d.close and d.confidence >= self.cfg.auto_close_min_confidence and \
                (missing := fields.missing_to_solve(self.zd, ticket, changes)):
            add_tags.append(TAG_SUGGEST_CLOSE)
            note.append(f"Would close ({d.close_reason}) but these fields need filling first: {', '.join(missing)}")
        elif d.close and d.confidence >= self.cfg.auto_close_min_confidence:
            changes["status"] = "solved"
            if not ticket.get("assignee_id"):
                changes["assignee_id"] = self.zd.me["id"]  # Zendesk won't solve unassigned tickets
            note.append(f"Solving: {d.close_reason}")
        elif d.close:
            add_tags.append(TAG_SUGGEST_CLOSE)
            note.append(f"Would close ({d.close_reason}) but confidence is below "
                        f"{self.cfg.auto_close_min_confidence}; leaving it for you.")

        changes["additional_tags"] = add_tags
        if remove_tags:
            changes["remove_tags"] = remove_tags
        return self.write(tid, "\n".join(note), changes)

    def write(self, tid: int, note: str, changes: dict) -> str | None:
        """Make the ticket changes (or just log them in dry run). Returns the new updated_at.
        Notes are always internal: the background agent never writes to customers."""
        self.audit.log("ticket_update", ticket=tid, dry_run=self.cfg.dry_run, changes=changes, note=note,
                       summary=_summarize(changes))
        if self.cfg.dry_run:
            return None
        # Status goes with the last update so the internal note lands before the ticket is solved.
        status_changes = {k: changes.pop(k) for k in ("status", "assignee_id") if k in changes}
        t = self.zd.note(tid, note, public=False, **changes)
        if status_changes:
            t = self.zd.update(tid, status_changes)
        return t["updated_at"]

    # --- instructions you leave as internal notes ---

    def follow_notes(self, ticket: dict, comments: list[dict], state: dict) -> bool:
        """Your internal notes starting with #agent are carried out; #rule notes become proposed
        rules in `review`. Only your own notes count, never customers' or other agents'.
        Returns True if any #agent instruction ran."""
        handled = state.setdefault("notes_handled", [])
        ran = False
        for c in comments:
            body = (c.get("plain_body") or c.get("body") or "").strip()
            lower = body.lower()
            if c.get("public") or c.get("author_id") != self.zd.me["id"] or c["id"] in handled:
                continue
            if lower.startswith(RULE_NOTE_PREFIX):
                instruction = body[len(RULE_NOTE_PREFIX):].strip(" :")
                self.queue.add("rule", ticket=ticket["id"], subject=ticket.get("subject"), instruction=instruction)
                self.audit.log("rule_proposed", ticket=ticket["id"], summary=instruction[:80])
            elif lower.startswith(COMMAND_NOTE_PREFIX):
                instruction = body[len(COMMAND_NOTE_PREFIX):].strip(" :")
                commander = CommandAgent(self.cfg, self.zd, self.exchange_page, self.audit, self.queue, self.book)
                reply = commander.handle(instruction, ticket_id=ticket["id"])
                self.audit.log("command_done", ticket=ticket["id"], summary=reply[:200])
                if not self.cfg.dry_run:
                    self.zd.note(ticket["id"], f"Agent: {reply}")
                ran = True
            else:
                continue
            handled.append(c["id"])
            self.state.save(state)
        return ran

    # --- approvals ---

    def run_approved(self) -> None:
        for item in self.queue.with_status("approved", "permission"):
            if self.exchange_page is None:
                return
            rehearsal = self.cfg.dry_run
            result = ExchangeOperator(self.exchange_page, self.cfg, self.audit, rehearsal).execute(item)
            if rehearsal:
                self.queue.update(item["id"], status="rehearsed", result=result)
                continue
            ok = result["success"] and result["verified"]
            self.queue.update(item["id"], status="done" if ok else "failed", result=result)
            tid = item["ticket"]
            if ok:
                note = f"Done [{item['id']}]: {describe(item['request'])}\n{result['summary']}"
                changes = {}
                if not self.queue.open_for_ticket(tid):
                    changes["remove_tags"] = [TAG_PENDING]
                    if self.cfg.solve_after_permission_change:
                        ticket = self.zd.get_ticket(tid)
                        missing = fields.missing_to_solve(self.zd, ticket)
                        if missing:
                            note += f"\nNot solving yet: fill in {', '.join(missing)} first."
                        else:
                            changes["status"] = "solved"
                            changes["assignee_id"] = ticket.get("assignee_id") or self.zd.me["id"]
                self._record_seen(tid, self.write(tid, note, changes))
            else:
                note = f"Couldn't complete [{item['id']}]: {describe(item['request'])}\n{result['summary']}"
                self._record_seen(tid, self.write(tid, note, {"additional_tags": [TAG_HUMAN], "remove_tags": [TAG_PENDING]}))

    def report_rejections(self) -> None:
        for item in self.queue.with_status("rejected", "permission"):
            if item.get("reported"):
                continue
            tid = item["ticket"]
            note = f"Not approved [{item['id']}]: {describe(item['request'])}\nReason: {item.get('reason') or '(none given)'}"
            changes = {"additional_tags": [TAG_HUMAN]}
            if len(self.queue.open_for_ticket(tid)) == 0:
                changes["remove_tags"] = [TAG_PENDING]
            self._record_seen(tid, self.write(tid, note, changes))
            self.queue.update(item["id"], reported=True)

    def _record_seen(self, tid: int, updated_at: str | None) -> None:
        if updated_at:
            state = self.state.load()
            state["seen"][str(tid)] = updated_at
            self.state.save(state)


def _summarize(changes: dict) -> str:
    parts = [f"{k}={v}" for k, v in changes.items() if k not in ("additional_tags", "remove_tags")]
    tags = [t for t in changes.get("additional_tags", []) if t != TAG_TRIAGED]
    if tags:
        parts.append("+tags " + ",".join(tags))
    return "; ".join(parts) or "note only"
