"""Your rules: stored in rules.yaml, learned from plain-language instructions.

Nothing is saved until you confirm it. Each rule records what you originally said,
and when the project is a git repo every change is committed so it can be undone.
"""

import datetime
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from . import llm

Area = Literal["triage", "closing", "permissions", "general"]


class Rule(BaseModel):
    id: str
    applies_to: Area
    when: str
    then: str
    examples: list[str] = []
    widens_access: bool = False
    source: str = ""
    added: str = ""
    enabled: bool = True


class Conflict(BaseModel):
    rule_id: str
    explanation: str


class ProposedRule(BaseModel):
    understood: bool
    clarifying_question: str | None
    applies_to: Area
    when: str
    then: str
    examples: list[str]
    widens_access: bool
    conflicts: list[Conflict]


class RuleBook:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Rule]:
        if not self.path.exists():
            return []
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return [Rule(**r) for r in data.get("rules", [])]

    def active(self) -> list[Rule]:
        return [r for r in self.load() if r.enabled]

    def save(self, rules: list[Rule], message: str) -> None:
        header = "# Rules the agent follows. Add them with `python -m triage_agent teach \"...\"`.\n"
        body = yaml.safe_dump({"rules": [r.model_dump() for r in rules]}, sort_keys=False, allow_unicode=True)
        self.path.write_text(header + body, encoding="utf-8")
        _git_commit(self.path, message)

    def next_id(self) -> str:
        nums = [int(r.id.split("-")[1]) for r in self.load() if r.id.startswith("r-") and r.id[2:].isdigit()]
        return f"r-{max(nums, default=0) + 1:03d}"

    def as_prompt(self) -> str:
        rules = self.active()
        if not rules:
            return "(no rules yet)"
        return "\n".join(f"[{r.id}] ({r.applies_to}) WHEN {r.when} THEN {r.then}" for r in rules)


TEACH_SYSTEM = """You turn a support lead's plain-language instruction into one precise rule for a
Zendesk triage agent. The agent triages tickets, closes them, and handles Exchange mailbox
permission requests.

- `when`: the condition, specific enough that two people would agree whether a ticket matches.
- `then`: exactly what the agent should do.
- `examples`: 1-3 short made-up tickets the rule would affect.
- `widens_access`: true if following the rule could grant mailbox or group access, or approve
  access changes, with less human review than before.
- `conflicts`: existing rules that would contradict this one for some ticket, by id.
- If the instruction is too vague to act on, set understood=false and ask one clarifying question.
Keep the person's intent; don't add requirements they didn't ask for."""


def propose(book: RuleBook, cfg, instruction: str, context: str = "") -> ProposedRule:
    user = f"Existing rules:\n{book.as_prompt()}\n\nNew instruction:\n{instruction}"
    if context:
        user += f"\n\nContext (the ticket or decision this was said about):\n{context}"
    return llm.parse(cfg.models.hard, "medium", TEACH_SYSTEM, user, ProposedRule)


def teach_interactive(book: RuleBook, cfg, instruction: str, context: str = "", ask=input) -> Rule | None:
    """Propose a rule, show it, and save it only if you confirm."""
    while True:
        p = propose(book, cfg, instruction, context)
        if p.understood:
            break
        answer = ask(f"\n{p.clarifying_question}\n> ").strip()
        if not answer:
            print("Not saved.")
            return None
        instruction = f"{instruction}\n(Clarification: {answer})"

    print("\nHere's the rule as I understood it:")
    print(f"  Applies to: {p.applies_to}")
    print(f"  When:       {p.when}")
    print(f"  Then:       {p.then}")
    for ex in p.examples:
        print(f"  e.g.        {ex}")

    rules = book.load()
    by_id = {r.id: r for r in rules}
    disable: list[str] = []
    for c in p.conflicts:
        old = by_id.get(c.rule_id)
        if not old or not old.enabled:
            continue
        print(f"\nConflicts with [{old.id}] WHEN {old.when} THEN {old.then}\n  {c.explanation}")
        choice = ask("Which wins? [n]ew rule / [o]ld rule / keep [b]oth: ").strip().lower()[:1]
        if choice == "o":
            print("Kept the old rule. Not saved.")
            return None
        if choice == "n":
            disable.append(old.id)

    if p.widens_access:
        print("\nWARNING: this rule could grant access with less review than today.")
        if ask("Type 'yes' to save it anyway: ").strip().lower() != "yes":
            print("Not saved.")
            return None
    elif ask("\nSave this rule? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Not saved.")
        return None

    rule = Rule(
        id=book.next_id(),
        applies_to=p.applies_to,
        when=p.when,
        then=p.then,
        examples=p.examples,
        widens_access=p.widens_access,
        source=instruction,
        added=datetime.date.today().isoformat(),
    )
    for r in rules:
        if r.id in disable:
            r.enabled = False
    book.save(rules + [rule], f"Add rule {rule.id}: {instruction[:60]}")
    print(f"Saved as {rule.id}." + (f" Disabled: {', '.join(disable)}." if disable else ""))
    return rule


def _git_commit(path: Path, message: str) -> None:
    try:
        cwd = path.parent
        if subprocess.run(["git", "rev-parse"], cwd=cwd, capture_output=True).returncode != 0:
            return
        subprocess.run(["git", "add", path.name], cwd=cwd, capture_output=True)
        subprocess.run(["git", "commit", "-m", message, "--", path.name], cwd=cwd, capture_output=True)
    except OSError:
        pass
