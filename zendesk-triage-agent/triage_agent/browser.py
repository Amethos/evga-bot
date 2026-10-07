"""A real browser with a saved profile, so your Zendesk and Microsoft logins persist."""

from contextlib import contextmanager

from playwright.sync_api import BrowserContext, sync_playwright


@contextmanager
def open_browser(cfg, headless: bool | None = None):
    """Yield a persistent browser context. Only one process can use the profile at a time."""
    with sync_playwright() as p:
        ctx: BrowserContext = p.chromium.launch_persistent_context(
            str(cfg.browser_profile_dir),
            channel=cfg.browser_channel or None,
            headless=cfg.headless if headless is None else headless,
            viewport={"width": 1400, "height": 900},
        )
        try:
            yield ctx
        finally:
            ctx.close()
