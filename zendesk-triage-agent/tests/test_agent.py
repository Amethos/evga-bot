"""Offline tests: a fake Zendesk and a fake Exchange page served locally, driven by a real
headless browser, with Claude's answers scripted. No API key or network needed."""

import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from anthropic.lib._parse._transform import transform_schema
from playwright.sync_api import sync_playwright

from triage_agent import decide as decide_mod
from triage_agent import exchange as exchange_mod
from triage_agent import rules as rules_mod
from triage_agent.agent import TAG_HUMAN, TAG_PENDING, TAG_SUGGEST_CLOSE, Agent
from triage_agent.command import DRAFT_HEADER, CommandAgent
from triage_agent.config import Config
from triage_agent.decide import FieldValue, PermissionRequest, TriageDecision
from triage_agent.rules import ProposedRule, RuleBook, teach_interactive
from triage_agent.store import Queue

ME = 900
CSRF = "csrf-123"
CATEGORY = 50

# Like a real Zendesk: Category is a dropdown that must be filled before a ticket can be solved.
TICKET_FIELDS = [
    {"id": 1, "type": "subject", "title": "Subject", "required": True, "active": True},
    {"id": 2, "type": "priority", "title": "Priority", "required": False, "active": True},
    {"id": CATEGORY, "type": "tagger", "title": "Category", "required": True, "active": True,
     "custom_field_options": [
         {"name": "Software::Adobe", "value": "software__adobe"},
         {"name": "Hardware::Printer", "value": "hardware__printer"},
         {"name": "Account::Mailbox access", "value": "account__mailbox"},
         {"name": "Spam", "value": "spam"},
     ]},
    {"id": 60, "type": "text", "title": "Old field", "required": False, "active": False},
]


def cat(value):
    return [{"id": CATEGORY, "value": value}]


TICKETS = {
    1: {"id": 1, "subject": "WIN BIG $$$", "status": "new", "priority": None, "requester_id": 11,
        "group_id": None, "assignee_id": None, "tags": [], "updated_at": "2026-10-07T10:00:00Z"},
    2: {"id": 2, "subject": "Need access to finance mailbox", "status": "open", "priority": "normal",
        "requester_id": 12, "group_id": None, "assignee_id": None, "tags": [], "custom_fields": cat("account__mailbox"),
        "updated_at": "2026-10-07T10:01:00Z"},
    3: {"id": 3, "subject": "Legal hold request", "status": "new", "priority": None, "requester_id": 13,
        "group_id": None, "assignee_id": None, "tags": [], "updated_at": "2026-10-07T10:02:00Z"},
    4: {"id": 4, "subject": "Acrobat keeps crashing", "status": "open", "priority": "normal", "requester_id": 14,
        "description": "Adobe Acrobat crashes when I open PDFs. Reinstall fixed it, thanks!",
        "group_id": 7, "assignee_id": ME, "tags": [], "custom_fields": cat(None), "updated_at": "2026-10-07T10:03:00Z"},
    5: {"id": 5, "subject": "Printer jam 3rd floor", "status": "open", "priority": "normal", "requester_id": 15,
        "description": "The 3rd floor printer is jammed again.",
        "group_id": 7, "assignee_id": ME, "tags": [], "custom_fields": cat(None), "updated_at": "2026-10-07T10:04:00Z"},
}
COMMENTS = {
    1: [{"id": 101, "author_id": 11, "public": True, "body": "Click here to win"},
        {"id": 102, "author_id": ME, "public": False, "body": "#rule spam from unknown senders can be closed"}],
    2: [{"id": 201, "author_id": 12, "public": True, "body": "Please give me Full Access to finance@corp.com"}],
    3: [{"id": 301, "author_id": 13, "public": True, "body": "Our lawyers need a hold on all mail"}],
    4: [{"id": 401, "author_id": 14, "public": True, "body": "Adobe Acrobat crashes. Reinstall fixed it, thanks!"}],
    5: [{"id": 501, "author_id": 15, "public": True, "body": "The 3rd floor printer is jammed again."}],
}

EAC_PAGE = """<!doctype html><title>Exchange admin center</title>
<h1>Mailboxes</h1>
<button onclick="document.getElementById('panel').hidden=false">Manage mailbox delegation</button>
<div id="panel" hidden>
  <label>User to add <input id="u"></label>
  <button onclick="document.getElementById('list').textContent='Full Access: '+document.getElementById('u').value">Save</button>
  <button>Delete mailbox</button>
</div>
<p id="list">Full Access: (none)</p>"""


class FakeZendesk(BaseHTTPRequestHandler):
    updates: list = []
    tickets: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, status, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/agent/", "/eac"):
            return self._send(200, (EAC_PAGE if path == "/eac" else "<title>Zendesk</title>").encode(), "text/html")
        if path == "/api/v2/users/me.json":
            return self._send(200, {"user": {"id": ME, "name": "Lead", "role": "admin", "authenticity_token": CSRF}})
        if path == "/api/v2/search.json":
            results = list(FakeZendesk.tickets.values())
            if "assignee%3Ame" in self.path:
                results = [t for t in results if t["assignee_id"] == ME]
            return self._send(200, {"results": results})
        if path == "/api/v2/ticket_fields.json":
            return self._send(200, {"ticket_fields": TICKET_FIELDS})
        if path == "/api/v2/users/search.json":
            return self._send(200, {"users": [{"id": 950, "name": "Dana", "email": "dana@corp.com", "role": "agent"}]})
        if path == "/api/v2/groups/assignable.json":
            return self._send(200, {"groups": [{"id": 7, "name": "IT Support"}]})
        if path.startswith("/api/v2/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[4])
            users = [{"id": 10 + tid, "name": f"Customer {tid}", "email": f"c{tid}@corp.com", "role": "end-user"},
                     {"id": ME, "name": "Lead", "email": "lead@corp.com", "role": "admin"}]
            return self._send(200, {"comments": list(reversed(COMMENTS[tid])), "users": users})
        if path.startswith("/api/v2/tickets/"):
            return self._send(200, {"ticket": FakeZendesk.tickets[int(path.split("/")[4].split(".")[0])]})
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.headers.get("X-CSRF-Token") != CSRF:
            return self._send(403, {"error": "bad csrf"})
        tid = int(self.path.split("/")[4].split(".")[0])
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        changes = body["ticket"]
        t = FakeZendesk.tickets[tid]
        category = {cf["id"]: cf["value"] for cf in changes.get("custom_fields", t.get("custom_fields") or [])}
        if changes.get("status") == "solved" and not (category.get(CATEGORY) or
                                                      {c["id"]: c["value"] for c in t.get("custom_fields") or []}.get(CATEGORY)):
            return self._send(422, {"error": "RecordInvalid", "details": {"Category": "required to solve"}})
        FakeZendesk.updates.append((tid, changes))
        if changes.get("custom_fields"):
            t["custom_fields"] = changes["custom_fields"]
        if changes.get("status"):
            t["status"] = changes["status"]
        t["updated_at"] = f"2026-10-07T11:00:{len(FakeZendesk.updates):02d}Z"
        self._send(200, {"ticket": t})


@pytest.fixture()
def env(tmp_path):
    FakeZendesk.updates = []
    FakeZendesk.tickets = copy.deepcopy(TICKETS)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeZendesk)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = Config(zendesk_subdomain="x", exchange_admin_url=base + "/eac", browser_channel="",
                 dry_run=False, rules_file=tmp_path / "rules.yaml", data_dir=tmp_path)
    with sync_playwright() as p:
        # CHROMIUM_PATH lets CI point at a preinstalled browser instead of `playwright install`.
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = browser.new_page()
        from triage_agent.zendesk import Zendesk
        zd = Zendesk(page, base)
        zd.connect()
        yield SimpleNamespace(cfg=cfg, zd=zd, ex_page=browser.new_page(), tmp=tmp_path)
        browser.close()
    server.shutdown()


def decision(**kw):
    base = dict(category="general", priority=None, group=None, fields=[], add_tags=[], remove_tags=[], close=False,
                close_reason=None, permission_requests=[], needs_human=False,
                needs_human_reason=None, confidence=0.95, applied_rule_ids=[], reasoning="because")
    return TriageDecision(**{**base, **kw})


CANNED = {
    "WIN BIG": decision(category="spam", close=True, close_reason="spam", priority="low", group="IT Support",
                        fields=[FieldValue(name="Category", value="Spam")]),
    "finance mailbox": decision(category="access", permission_requests=[PermissionRequest(
        action="grant", permission="full_access", target="finance@corp.com", user="c2@corp.com", details="")]),
    "Legal hold": decision(category="legal", needs_human=True, needs_human_reason="legal request"),
    "Acrobat": decision(category="software", close=True, close_reason="fixed"),  # no Category given
    "Printer jam": decision(category="hardware"),
}


def fake_parse(model, effort, system, user, schema):
    return next(d for key, d in CANNED.items() if key in user)


def test_schemas_are_accepted_by_structured_outputs():
    for model in (TriageDecision, ProposedRule):
        assert transform_schema(model.model_json_schema())["type"] == "object"


def test_triage_cycle_updates_tickets(env, monkeypatch):
    monkeypatch.setattr(decide_mod.llm, "parse", fake_parse)
    agent = Agent(env.cfg, env.zd, env.ex_page)
    agent.cycle()

    by_ticket = {}
    for tid, body in FakeZendesk.updates:
        by_ticket.setdefault(tid, []).append(body)

    # Spam: triaged, note added, then solved and assigned to me.
    first, last = by_ticket[1]
    assert first["priority"] == "low" and first["group_id"] == 7 and first["custom_fields"] == cat("spam")
    assert first["comment"]["public"] is False
    assert last == {"status": "solved", "assignee_id": ME}

    # Access request: queued for approval, ticket tagged, nothing solved.
    [upd] = by_ticket[2]
    assert TAG_PENDING in upd["additional_tags"] and "status" not in upd
    [item] = Queue(env.tmp / "queue.json").with_status("pending", "permission")
    assert item["request"]["target"] == "finance@corp.com"

    # Legal: flagged for a person.
    [upd] = by_ticket[3]
    assert TAG_HUMAN in upd["additional_tags"] and "status" not in upd

    # Would close the Acrobat ticket, but Category is required and wasn't chosen: suggest instead.
    [upd] = by_ticket[4]
    assert "status" not in upd and TAG_SUGGEST_CLOSE in upd["additional_tags"]
    assert "Category" in upd["comment"]["body"]

    # The #rule note became a proposed rule.
    [rule] = Queue(env.tmp / "queue.json").with_status("pending", "rule")
    assert "spam" in rule["instruction"]

    # A second pass sees nothing new and makes no calls.
    n = len(FakeZendesk.updates)
    agent.cycle()
    assert len(FakeZendesk.updates) == n


def test_dry_run_changes_nothing(env, monkeypatch):
    monkeypatch.setattr(decide_mod.llm, "parse", fake_parse)
    env.cfg.dry_run = True
    Agent(env.cfg, env.zd, env.ex_page).cycle()
    assert FakeZendesk.updates == []


class ScriptedClaude:
    """Stands in for the Claude client: returns one scripted tool call per request."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen_results = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        last = kw["messages"][-1]["content"]
        if isinstance(last, list):
            self.seen_results.extend(last)
        name, args = self.steps.pop(0)
        if name == "text":
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=args)], stop_reason="end_turn")
        block = SimpleNamespace(type="tool_use", id=f"t{len(self.steps)}", name=name, input=args)
        return SimpleNamespace(content=[block], stop_reason="tool_use")


def exchange_item(env):
    q = Queue(env.tmp / "queue.json")
    return q.add("permission", ticket=2, subject="x", status="approved", request={
        "action": "grant", "permission": "full_access", "target": "finance@corp.com", "user": "c2@corp.com", "details": ""})


def test_exchange_operator_makes_and_verifies_change(env, monkeypatch):
    claude = ScriptedClaude([
        ("click", {"role": "button", "name": "Manage mailbox delegation", "index": 0}),
        ("fill", {"role": "textbox", "name": "User to add", "index": 0, "text": "c2@corp.com"}),
        ("click", {"role": "button", "name": "Delete mailbox", "index": 0}),  # must be blocked
        ("click", {"role": "button", "name": "Save", "index": 0}),
        ("finish", {"success": True, "verified": True, "summary": "Full Access added"}),
    ])
    monkeypatch.setattr(exchange_mod.llm, "client", lambda: claude)
    exchange_item(env)
    agent = Agent(env.cfg, env.zd, env.ex_page)
    agent.run_approved()

    assert "Full Access: c2@corp.com" in env.ex_page.content()
    blocked = [r for r in claude.seen_results if r["is_error"]]
    assert len(blocked) == 1 and "destructive" in blocked[0]["content"]
    [done] = Queue(env.tmp / "queue.json").with_status("done")
    assert done["result"]["verified"]
    (tid, note), (tid2, final) = FakeZendesk.updates[-2:]
    assert tid == tid2 == 2
    assert "Done" in note["comment"]["body"] and note["remove_tags"] == [TAG_PENDING]
    assert final == {"status": "solved", "assignee_id": ME}


def test_rehearsal_cannot_save(env, monkeypatch):
    claude = ScriptedClaude([
        ("click", {"role": "button", "name": "Manage mailbox delegation", "index": 0}),
        ("click", {"role": "button", "name": "Save", "index": 0}),
        ("finish", {"success": True, "verified": False, "summary": "Stopped at Save"}),
    ])
    monkeypatch.setattr(exchange_mod.llm, "client", lambda: claude)
    env.cfg.dry_run = True
    exchange_item(env)
    Agent(env.cfg, env.zd, env.ex_page).run_approved()
    assert "Full Access: (none)" in env.ex_page.content()
    assert any("rehearsal" in r["content"] for r in claude.seen_results if r["is_error"])
    assert Queue(env.tmp / "queue.json").with_status("rehearsed")
    assert FakeZendesk.updates == []


def test_goto_outside_microsoft_is_blocked(env):
    op = exchange_mod.ExchangeOperator(env.ex_page, env.cfg, None, rehearsal=False)
    text, is_error = op.run_tool("goto", {"url": "https://evil.example.com"}, "grant")
    assert is_error and "Blocked" in text


def proposed(**kw):
    base = dict(understood=True, clarifying_question=None, applies_to="closing",
                when="ticket is spam from an unknown sender", then="close it", examples=["'WIN BIG'"],
                widens_access=False, conflicts=[])
    return ProposedRule(**{**base, **kw})


def test_teach_saves_only_after_confirmation(tmp_path, monkeypatch):
    cfg = Config(zendesk_subdomain="x", rules_file=tmp_path / "rules.yaml", data_dir=tmp_path)
    book = RuleBook(cfg.rules_file)
    monkeypatch.setattr(rules_mod, "propose", lambda *a, **k: proposed())

    assert teach_interactive(book, cfg, "close spam", ask=lambda _: "n") is None
    assert book.load() == []

    rule = teach_interactive(book, cfg, "close spam", ask=lambda _: "y")
    assert rule.id == "r-001" and book.load()[0].source == "close spam"
    assert "[r-001] (closing) WHEN ticket is spam" in book.as_prompt()


def test_teach_conflict_disables_old_rule(tmp_path, monkeypatch):
    cfg = Config(zendesk_subdomain="x", rules_file=tmp_path / "rules.yaml", data_dir=tmp_path)
    book = RuleBook(cfg.rules_file)
    monkeypatch.setattr(rules_mod, "propose", lambda *a, **k: proposed())
    teach_interactive(book, cfg, "close spam", ask=lambda _: "y")

    monkeypatch.setattr(rules_mod, "propose", lambda *a, **k: proposed(
        when="any ticket", then="never close", conflicts=[rules_mod.Conflict(rule_id="r-001", explanation="x")]))
    answers = iter(["n", "y"])  # new rule wins, then save
    teach_interactive(book, cfg, "never close anything", ask=lambda _: next(answers))
    rules = {r.id: r for r in book.load()}
    assert not rules["r-001"].enabled and rules["r-002"].enabled


def test_access_widening_rule_needs_explicit_yes(tmp_path, monkeypatch):
    cfg = Config(zendesk_subdomain="x", rules_file=tmp_path / "rules.yaml", data_dir=tmp_path)
    book = RuleBook(cfg.rules_file)
    monkeypatch.setattr(rules_mod, "propose", lambda *a, **k: proposed(widens_access=True))
    assert teach_interactive(book, cfg, "auto-approve finance", ask=lambda _: "y") is None
    assert teach_interactive(book, cfg, "auto-approve finance", ask=lambda _: "yes") is not None


def upd(ticket_id, **kw):
    base = dict(ticket_id=ticket_id, reason="", status=None, priority=None, group=None, assignee_email=None,
                type=None, fields=[], add_tags=[], remove_tags=[], internal_note=None, draft_reply=None,
                send_reply=None)
    return {**base, **kw}


def update(*updates):
    return ("update_tickets", {"updates": list(updates)})


def commander(env, ask):
    agent = Agent(env.cfg, env.zd, env.ex_page)
    return CommandAgent(env.cfg, env.zd, env.ex_page, agent.audit, agent.queue, agent.book, ask=ask)


def run_chat(env, monkeypatch, steps, text, ask):
    claude = ScriptedClaude(steps)
    monkeypatch.setattr(exchange_mod.llm, "client", lambda: claude)
    reply = commander(env, ask).handle(text)
    return claude, reply


def test_my_queue_shows_what_each_ticket_needs(env):
    out = json.loads(commander(env, ask=None).my_queue())
    assert [t["id"] for t in out] == [4, 5]
    adobe = out[0]
    assert "Adobe" in adobe["description"] and adobe["needs_before_solving"] == ["Category"]


def test_get_ticket_lists_options_for_required_fields(env):
    text, _ = commander(env, ask=None).run_tool("get_ticket", {"ticket_id": 4})
    assert "Category: (empty)  [required to solve]" in text and "Software::Adobe" in text
    assert "Old field" not in text


def test_close_all_my_tickets_fills_category_and_confirms_once(env, monkeypatch):
    prompts = []
    claude, reply = run_chat(env, monkeypatch, [
        ("my_queue", {}),
        update(upd(4, status="solved", fields=[{"name": "Category", "value": "Software::Adobe"}], reason="reinstall fixed it"),
               upd(5, status="solved", fields=[{"name": "category", "value": "Printer"}], reason="⚠ customer may still be waiting")),
        ("text", "Closed both."),
    ], "close all my tickets", ask=lambda q: prompts.append(q) or "")
    assert len(prompts) == 1  # one confirmation for the whole batch
    assert "2 tickets to change" in prompts[0] and "Category = Software::Adobe" in prompts[0]
    assert "customer may still be waiting" in prompts[0]
    assert {tid: b["custom_fields"] for tid, b in FakeZendesk.updates} == {
        4: cat("software__adobe"), 5: cat("hardware__printer")}  # "Printer" matched Hardware::Printer
    assert all(b["status"] == "solved" for _, b in FakeZendesk.updates)


def test_cannot_close_without_required_fields(env, monkeypatch):
    claude, _ = run_chat(env, monkeypatch, [update(upd(4, status="solved")), ("text", "Needs a category.")],
                         "close the adobe one", ask=lambda q: "")
    assert FakeZendesk.updates == []
    assert "still needs: Category" in claude.seen_results[0]["content"]


def test_bad_option_lists_the_valid_ones(env, monkeypatch):
    claude, _ = run_chat(env, monkeypatch, [
        update(upd(4, status="solved", fields=[{"name": "Category", "value": "Photoshop"}])), ("text", "x")],
        "close it", ask=lambda q: "")
    assert "isn't an option" in claude.seen_results[0]["content"] and "Software::Adobe" in claude.seen_results[0]["content"]


def test_chat_escalate_and_assign(env, monkeypatch):
    run_chat(env, monkeypatch, [
        update(upd(5, priority="urgent", group="it support", assignee_email="dana")), ("text", "Escalated.")],
        "escalate the printer one to IT and give it to Dana", ask=lambda q: "")
    [(tid, body)] = FakeZendesk.updates
    assert tid == 5 and body == {"priority": "urgent", "assignee_id": 950}  # already in IT Support, so no group change


def test_chat_declined_change_is_not_made(env, monkeypatch):
    claude, _ = run_chat(env, monkeypatch, [update(upd(5, priority="urgent")), ("text", "Okay.")],
                         "escalate the printer one", ask=lambda q: "n")
    assert FakeZendesk.updates == []
    assert "declined" in claude.seen_results[0]["content"]


def test_draft_reply_is_internal_and_marked(env, monkeypatch):
    run_chat(env, monkeypatch, [update(upd(5, draft_reply="Hi, a technician is on the way.")), ("text", "Drafted.")],
             "draft a reply on the printer ticket", ask=lambda q: "")
    [(_, body)] = FakeZendesk.updates
    assert body["comment"] == {"body": DRAFT_HEADER + "Hi, a technician is on the way.", "public": False}


def test_sending_to_customer_needs_typed_yes(env, monkeypatch):
    prompts = []
    run_chat(env, monkeypatch, [update(upd(5, send_reply="On our way.")), ("text", "Not sent.")],
             "send them a reply", ask=lambda q: prompts.append(q) or "")  # Enter alone isn't enough
    assert FakeZendesk.updates == [] and "SEND TO CUSTOMER" in prompts[0] and "Type y" in prompts[0]

    run_chat(env, monkeypatch, [update(upd(5, send_reply="On our way.")), ("text", "Sent.")],
             "send them a reply", ask=lambda q: "y")
    [(_, body)] = FakeZendesk.updates
    assert body["comment"] == {"body": "On our way.", "public": True}


def test_agent_note_never_messages_customer(env, monkeypatch):
    run_chat(env, monkeypatch, [update(upd(5, send_reply="On our way.")), ("text", "Drafted.")],
             "reply to them", ask=None)
    [(_, body)] = FakeZendesk.updates
    assert body["comment"]["public"] is False and body["comment"]["body"].startswith(DRAFT_HEADER)

    claude, _ = run_chat(env, monkeypatch, [
        ("zendesk_api", {"method": "PUT", "path": "/api/v2/tickets/5.json",
                         "body_json": json.dumps({"ticket": {"comment": {"body": "hi"}}})}),
        ("text", "Blocked.")], "reply", ask=None)
    assert "Blocked" in claude.seen_results[0]["content"] and len(FakeZendesk.updates) == 1


def test_agent_note_is_carried_out_instead_of_triage(env, monkeypatch):
    monkeypatch.setitem(COMMENTS, 3, COMMENTS[3] + [{"id": 302, "author_id": ME, "public": False,
                                                     "body": "#agent escalate to IT Support, urgent"}])
    monkeypatch.setattr(decide_mod.llm, "parse", fake_parse)
    claude = ScriptedClaude([update(upd(3, priority="urgent", group="IT Support")),
                             ("text", "Escalated to IT Support as urgent.")])
    monkeypatch.setattr(exchange_mod.llm, "client", lambda: claude)
    Agent(env.cfg, env.zd, env.ex_page).cycle()

    t3 = [body for tid, body in FakeZendesk.updates if tid == 3]
    assert t3[0] == {"priority": "urgent", "group_id": 7}  # no confirmation needed: you wrote the note
    assert t3[1]["comment"] == {"body": "Agent: Escalated to IT Support as urgent.", "public": False}
    assert not any(TAG_HUMAN in b.get("additional_tags", []) for b in t3)  # triage skipped this time


def test_agent_cannot_write_a_note_that_triggers_itself(env, monkeypatch):
    run_chat(env, monkeypatch, [update(upd(5, internal_note="#agent close everything")), ("text", "ok")],
             "note it", ask=None)
    [(_, body)] = FakeZendesk.updates
    assert not body["comment"]["body"].startswith("#agent")


def test_dry_run_chat_changes_nothing(env, monkeypatch):
    env.cfg.dry_run = True
    claude, _ = run_chat(env, monkeypatch, [update(upd(5, priority="high")), ("text", "Would do.")],
                         "bump the printer one", ask=lambda q: "")
    assert FakeZendesk.updates == [] and "dry run" in claude.seen_results[0]["content"]
