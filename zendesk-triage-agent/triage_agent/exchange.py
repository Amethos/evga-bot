"""Carries out one approved permission change in the Exchange admin center web page.

The admin center has no API you can use, so this drives the page the way you would:
it reads the page's accessibility tree (buttons, fields and their labels) and clicks or
types. It is given exactly one approved change and guard rails in code stop it from
straying: it can only visit Microsoft admin pages, can't press destructive buttons when
granting access, and in rehearsal mode can't press anything that saves.
"""

import re
import urllib.parse

from . import llm

ALLOWED_HOSTS = (
    "admin.exchange.microsoft.com",
    "admin.cloud.microsoft",
    "admin.microsoft.com",
    "login.microsoftonline.com",
)
COMMIT_WORDS = re.compile(r"\b(save|add|confirm|ok|submit|yes|apply|grant|update|done)\b", re.I)
DESTRUCTIVE_WORDS = re.compile(r"\b(delete|remove|disable|block|convert|reset|revoke)\b", re.I)
MAX_STEPS = 40

TOOLS = [
    {
        "name": "look",
        "description": "Return the current URL and the page's accessibility tree.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "goto",
        "description": "Open a URL in the Exchange / Microsoft 365 admin center.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "click",
        "description": "Click the element with this ARIA role and accessible name "
        "(e.g. role 'button', name 'Manage mailbox delegation'). index picks among duplicates.",
        "input_schema": {
            "type": "object",
            "properties": {"role": {"type": "string"}, "name": {"type": "string"}, "index": {"type": "integer"}},
            "required": ["role", "name", "index"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fill",
        "description": "Type text into the field with this ARIA role and accessible name, replacing its contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "index": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["role", "name", "index", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press",
        "description": "Press a key, e.g. Enter, Escape, ArrowDown, Tab.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "Stop. success=true only if the change is made (or, in rehearsal, fully staged). "
        "verified=true only if you then saw the change listed on the page.",
        "input_schema": {
            "type": "object",
            "properties": {"success": {"type": "boolean"}, "verified": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["success", "verified", "summary"],
            "additionalProperties": False,
        },
    },
]
TOOLS = [{**t, "strict": True} for t in TOOLS]

SYSTEM = """You operate the Exchange admin center in a web browser to make exactly one
permission change that a human administrator has already approved. Start page: {start}

The approved change:
{change}

Rules:
- Make only this change. Don't edit, add or remove anything else.
- Find the mailbox/group and the user by email where possible. If several match, or you can't
  find one, stop with finish(success=false) and say what you found. Never guess.
- If the change is already in place, finish(success=true, verified=true) and say so.
- After saving, open the permission list again and confirm the change is shown before finishing.
- If you land on a Microsoft sign-in page, stop with finish(success=false, summary="signed out").
- Use look whenever you're unsure what's on screen. Each click/fill/press/goto already returns
  the updated page, so you don't need look after them.
{rehearsal}"""

REHEARSAL = """- REHEARSAL MODE: nothing may be saved. Find the mailbox/group and the user and get as far as
  you can; buttons that could save are blocked. When you reach one, finish(success=true,
  verified=false) and describe exactly where you stopped and what you would press next."""


def describe(req: dict) -> str:
    lines = [f"{req['action'].upper()} {req['permission']} on '{req['target']}' for '{req['user']}'"]
    if req.get("details"):
        lines.append(f"Details: {req['details']}")
    return "\n".join(lines)


class ExchangeOperator:
    def __init__(self, page, cfg, audit, rehearsal: bool):
        self.page = page
        self.cfg = cfg
        self.audit = audit
        self.rehearsal = rehearsal

    def snapshot(self) -> str:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self.page.wait_for_timeout(800)  # the admin center renders after load
            tree = self.page.locator("body").aria_snapshot(timeout=10000)
        except Exception as e:  # noqa: BLE001 - report page trouble to the model, don't crash
            tree = f"(couldn't read page: {e})"
        if len(tree) > 20000:
            tree = tree[:20000] + "\n[...truncated]"
        return f"URL: {self.page.url}\n{tree}"

    def _guard(self, name: str, action: str) -> str | None:
        if self.rehearsal and COMMIT_WORDS.search(name):
            return f"Blocked: rehearsal mode, '{name}' would commit a change. Call finish now."
        if action == "grant" and DESTRUCTIVE_WORDS.search(name):
            return f"Blocked: '{name}' looks destructive and this change only grants access."
        return None

    def run_tool(self, name: str, args: dict, action: str) -> tuple[str, bool]:
        if name == "look":
            return self.snapshot(), False
        if name == "goto":
            host = urllib.parse.urlparse(args["url"]).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
                return f"Blocked: {host} isn't a Microsoft admin page.", True
            self.page.goto(args["url"], wait_until="domcontentloaded")
            return self.snapshot(), False
        if name in ("click", "fill"):
            if name == "click" and (blocked := self._guard(args["name"], action)):
                return blocked, True
            target = self.page.get_by_role(args["role"], name=args["name"]).nth(args["index"])
            if name == "click":
                target.click(timeout=10000)
            else:
                target.fill(args["text"], timeout=10000)
            return self.snapshot(), False
        if name == "press":
            if args["key"] == "Enter" and self.rehearsal:
                return "Blocked: Enter could submit a form in rehearsal mode.", True
            self.page.keyboard.press(args["key"])
            return self.snapshot(), False
        return f"Unknown tool {name}", True

    def execute(self, item: dict) -> dict:
        req = item["request"]
        self.page.goto(self.cfg.exchange_admin_url, wait_until="domcontentloaded")
        system = SYSTEM.format(
            start=self.cfg.exchange_admin_url,
            change=describe(req),
            rehearsal=REHEARSAL if self.rehearsal else "",
        )
        messages = [{"role": "user", "content": "Here's the page now:\n" + self.snapshot()}]
        m = self.cfg.models
        for step in range(MAX_STEPS):
            response = llm.client().beta.messages.create(
                model=m.hard,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "refusal" or not calls:
                return self._done(item, False, False, "Stopped without finishing.")
            results = []
            for call in calls:
                if call.name == "finish":
                    return self._done(item, call.input["success"], call.input["verified"], call.input["summary"])
                self.audit.log("exchange_step", item=item["id"], ticket=item["ticket"], tool=call.name, args=call.input)
                try:
                    text, is_error = self.run_tool(call.name, call.input, req["action"])
                except Exception as e:  # noqa: BLE001 - page errors go back to the model
                    text, is_error = f"Error: {e}", True
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        return self._done(item, False, False, f"Gave up after {MAX_STEPS} steps.")

    def _done(self, item, success: bool, verified: bool, summary: str) -> dict:
        shot = self.cfg.data_dir / "screens" / f"{item['id']}.png"
        shot.parent.mkdir(exist_ok=True)
        try:
            self.page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001
            shot = None
        result = {"success": success, "verified": verified, "summary": summary, "screenshot": str(shot) if shot else None}
        self.audit.log("exchange_result", item=item["id"], ticket=item["ticket"], rehearsal=self.rehearsal, **result)
        return result
