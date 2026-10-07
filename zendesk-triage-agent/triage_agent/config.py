"""Settings, loaded from config.yaml next to the project."""

from pathlib import Path

import yaml
from pydantic import BaseModel


class Models(BaseModel):
    # The fast pass handles most tickets; anything uncertain or involving
    # access changes is re-decided by the "hard" settings.
    triage: str = "claude-opus-5-5"
    triage_effort: str = "low"
    hard: str = "claude-opus-5-5"
    hard_effort: str = "high"
    # Following your typed or #agent instructions.
    command: str = "claude-opus-5-5"
    command_effort: str = "low"


class Config(BaseModel):
    zendesk_subdomain: str
    exchange_admin_url: str = "https://admin.exchange.microsoft.com"

    # "msedge" uses the Edge that ships with Windows, so nothing extra to install.
    # Use "chrome" for Google Chrome, or "" for Playwright's bundled Chromium.
    browser_channel: str = "msedge"
    browser_profile_dir: Path = Path(".browser-profile")
    headless: bool = True

    poll_seconds: int = 60
    ticket_query: str = "type:ticket status<solved"
    # What "my tickets" / "my queue" means when you talk to it.
    my_queue_query: str = "type:ticket assignee:me status<solved"
    max_tickets_per_cycle: int = 25

    # Dry run: decide and log everything, change nothing.
    dry_run: bool = True
    auto_close_min_confidence: float = 0.85
    escalate_below_confidence: float = 0.7
    solve_after_permission_change: bool = True
    # In chat, confirm each change before it's made (Enter = yes). Deletes and Exchange changes always ask.
    confirm_writes: bool = True

    rules_file: Path = Path("rules.yaml")
    data_dir: Path = Path("data")
    models: Models = Models()

    @property
    def zendesk_url(self) -> str:
        return f"https://{self.zendesk_subdomain}.zendesk.com"


def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{path} not found. Copy config.example.yaml to config.yaml and fill it in.")
    cfg = Config(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))
    base = p.resolve().parent
    for field in ("browser_profile_dir", "rules_file", "data_dir"):
        value = getattr(cfg, field)
        if not value.is_absolute():
            setattr(cfg, field, base / value)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
