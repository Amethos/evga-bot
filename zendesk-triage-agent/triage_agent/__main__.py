"""Command line: python -m triage_agent <command>

  login            Open the browser so you can sign in to Zendesk and the Exchange admin center
  chat             Tell it what to do ("close 4512", "escalate 4513 to Tier 2"); triage keeps
                   running in the background between your messages
  run              Background only: triage, follow #agent notes, run approved changes, repeat
  once             One pass, then exit (good for trying it out)
  review           Go through what's waiting on you: permission changes and proposed rules
  teach "..."      Tell it a new rule in plain language
  rules            List the rules; `rules disable r-003` / `rules enable r-003`
  status           Counts of what's queued and recent activity
"""

import argparse
import json
import queue as queue_mod
import sys
import threading
import time

from .config import load_config
from .exchange import describe
from .rules import RuleBook, teach_interactive
from .store import Queue


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="triage_agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    chat_p = sub.add_parser("chat")
    chat_p.add_argument("--no-background", action="store_true", help="don't triage between messages")
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("review")
    teach = sub.add_parser("teach")
    teach.add_argument("instruction", nargs="+")
    rules = sub.add_parser("rules")
    rules.add_argument("action", nargs="?", choices=["disable", "enable"])
    rules.add_argument("rule_id", nargs="?")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "login":
        login(cfg)
    elif args.cmd == "chat":
        chat(cfg, background=not args.no_background)
    elif args.cmd in ("run", "once"):
        run(cfg, once=args.cmd == "once")
    elif args.cmd == "review":
        review(cfg)
    elif args.cmd == "teach":
        teach_interactive(RuleBook(cfg.rules_file), cfg, " ".join(args.instruction))
    elif args.cmd == "rules":
        manage_rules(cfg, args.action, args.rule_id)
    elif args.cmd == "status":
        status(cfg)


def login(cfg) -> None:
    from .browser import open_browser
    from .zendesk import Zendesk, ZendeskError

    with open_browser(cfg, headless=False) as ctx:
        zd_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        zd_page.goto(cfg.zendesk_url + "/agent/")
        ctx.new_page().goto(cfg.exchange_admin_url)
        input("Sign in to Zendesk and the Exchange admin center in the browser window "
              "(tick 'stay signed in' if asked), then press Enter here... ")
        try:
            me = Zendesk(zd_page, cfg.zendesk_url).connect()
            print(f"Zendesk: signed in as {me['name']} ({me['role']}).")
        except ZendeskError as e:
            print(f"Zendesk: {e}")
    print("Saved. The agent will reuse this login.")


def run(cfg, once: bool) -> None:
    from .agent import Agent
    from .browser import open_browser
    from .zendesk import Zendesk

    mode = "DRY RUN (no changes will be made)" if cfg.dry_run else "LIVE"
    print(f"Starting in {mode} mode. Ctrl+C to stop.")
    with open_browser(cfg) as ctx:
        zd_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        zd = Zendesk(zd_page, cfg.zendesk_url)
        me = zd.connect()
        print(f"Zendesk: working as {me['name']}.")
        agent = Agent(cfg, zd, exchange_page=ctx.new_page())
        try:
            agent.cycle() if once else agent.run_forever()
        except KeyboardInterrupt:
            print("Stopped.")


CHAT_HELP = """Type what you want done, e.g.
  close 4512, it's a duplicate of 4498
  escalate 4513 to Tier 2 and set it urgent
  give sam@corp.com Send As on finance@corp.com for ticket 4520
  from now on, password reset tickets go to the Service Desk group
Commands: /review  /status  /pause (background triage on/off)  /new (fresh conversation)  /quit"""


def chat(cfg, background: bool) -> None:
    from .agent import Agent
    from .browser import open_browser
    from .command import CommandAgent
    from .zendesk import Zendesk, ZendeskError

    # Keyboard input is read on its own thread; the browser is only touched from this one.
    lines: queue_mod.Queue = queue_mod.Queue()

    def reader():
        for line in sys.stdin:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def ask(prompt: str) -> str:
        print(prompt, end="", flush=True)
        answer = lines.get()
        if answer is None:
            raise SystemExit
        return answer

    with open_browser(cfg) as ctx:
        zd_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        zd = Zendesk(zd_page, cfg.zendesk_url)
        me = zd.connect()
        agent = Agent(cfg, zd, exchange_page=ctx.new_page())
        commander = CommandAgent(cfg, zd, agent.exchange_page, agent.audit, agent.queue, agent.book, ask=ask)
        mode = "DRY RUN (nothing will be changed)" if cfg.dry_run else "LIVE"
        print(f"Signed in to Zendesk as {me['name']}. {mode}. Background triage: {'on' if background else 'off'}.")
        print(CHAT_HELP)
        next_cycle = time.time()
        prompted = False
        while True:
            if background and time.time() >= next_cycle:
                try:
                    agent.cycle()
                except ZendeskError as e:
                    print(f"Zendesk: {e}")
                next_cycle = time.time() + cfg.poll_seconds
                prompted = False
            if not prompted:
                print("\nyou> ", end="", flush=True)
                prompted = True
            try:
                text = lines.get(timeout=1)
            except queue_mod.Empty:
                continue
            prompted = False
            if text is None or text.strip() in ("/quit", "/exit"):
                return
            text = text.strip()
            if not text:
                continue
            if text == "/review":
                review(cfg, ask=ask)
            elif text == "/status":
                status(cfg)
            elif text == "/pause":
                background = not background
                print(f"Background triage {'on' if background else 'off'}.")
            elif text == "/new":
                commander.reset()
                print("Started a fresh conversation.")
            elif text in ("/help", "?"):
                print(CHAT_HELP)
            else:
                try:
                    print("agent>", commander.handle(text))
                except ZendeskError as e:
                    print(f"Zendesk: {e}")
                except Exception as e:  # noqa: BLE001 - keep the chat alive
                    print(f"Something went wrong: {e!r}")


def review(cfg, ask=input) -> None:
    queue = Queue(cfg.data_dir / "queue.json")
    book = RuleBook(cfg.rules_file)
    items = [i for i in queue.all() if i["status"] in ("pending", "rehearsed", "failed")]
    if not items:
        print("Nothing waiting on you.")
        return
    if cfg.dry_run:
        print("Note: dry run is on, so approved changes are rehearsed (nothing saved), not made.\n")

    for item in items:
        print("=" * 70)
        print(f"[{item['id']}] ticket #{item.get('ticket')}: {item.get('subject')}")
        if item["kind"] == "rule":
            print(f"You left a #rule note: {item['instruction']}")
            rule = teach_interactive(book, cfg, item["instruction"], context=item.get("subject") or "", ask=ask)
            queue.update(item["id"], status="done" if rule else "rejected")
            continue

        print(describe(item["request"]))
        print(f"Why: {item.get('reasoning')}")
        if item.get("result"):
            r = item["result"]
            print(f"Last attempt ({item['status']}): {r['summary']}")
            if r.get("screenshot"):
                print(f"Screenshot: {r['screenshot']}")
        choice = ask("[a]pprove  [r]eject  [s]kip  [q]uit: ").strip().lower()[:1]
        if choice == "q":
            return
        if choice == "a":
            queue.update(item["id"], status="approved")
            print("Approved. The background agent will make the change on its next pass.")
        elif choice == "r":
            reason = ask("Why not? (this is noted on the ticket) ").strip()
            queue.update(item["id"], status="rejected", reason=reason)
            if reason and ask("Make that a rule for next time? [y/N]: ").strip().lower() in ("y", "yes"):
                context = f"Ticket #{item['ticket']} {item.get('subject')}\nRequest: {describe(item['request'])}"
                teach_interactive(book, cfg, reason, context=context, ask=ask)


def manage_rules(cfg, action, rule_id) -> None:
    book = RuleBook(cfg.rules_file)
    rules = book.load()
    if action:
        match = [r for r in rules if r.id == rule_id]
        if not match:
            sys.exit(f"No rule {rule_id}")
        match[0].enabled = action == "enable"
        book.save(rules, f"{action.capitalize()} rule {rule_id}")
        print(f"{rule_id} {action}d.")
        return
    if not rules:
        print('No rules yet. Add one with: python -m triage_agent teach "..."')
    for r in rules:
        flag = "" if r.enabled else "  (disabled)"
        warn = "  [widens access]" if r.widens_access else ""
        print(f"{r.id} ({r.applies_to}){flag}{warn}\n  WHEN {r.when}\n  THEN {r.then}")


def status(cfg) -> None:
    queue = Queue(cfg.data_dir / "queue.json")
    counts: dict[str, int] = {}
    for i in queue.all():
        counts[f"{i['kind']} {i['status']}"] = counts.get(f"{i['kind']} {i['status']}", 0) + 1
    print("Mode:", "dry run" if cfg.dry_run else "live")
    print("Queue:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "empty")
    log = cfg.data_dir / "audit.jsonl"
    if log.exists():
        print("Recent activity:")
        for line in log.read_text(encoding="utf-8").splitlines()[-10:]:
            r = json.loads(line)
            print(f"  {r['ts']} {r['kind']} {('#' + str(r['ticket'])) if 'ticket' in r else ''} {r.get('summary', '')}")


if __name__ == "__main__":
    main()
