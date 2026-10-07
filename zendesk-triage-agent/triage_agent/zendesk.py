"""Zendesk access through your signed-in browser tab.

The Zendesk agent interface loads its data from /api/v2 using your login session.
This client makes the same requests from inside that tab, so it needs no API token,
and it can only do what your own Zendesk account is allowed to do.
"""

import json
import time
import urllib.parse

_FETCH_JS = """
async ({method, path, body, csrf}) => {
  const headers = {"Accept": "application/json"};
  if (body !== null) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const r = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: body === null ? undefined : JSON.stringify(body),
  });
  return {status: r.status, text: await r.text(), retryAfter: r.headers.get("Retry-After")};
}
"""


class ZendeskError(Exception):
    pass


class Zendesk:
    def __init__(self, page, base_url: str):
        self.page = page
        self.base = base_url
        self.csrf = None
        self.me = None
        self._groups = None
        self._fields = None

    def connect(self) -> dict:
        if not self.page.url.startswith(self.base):
            self.page.goto(self.base + "/agent/", wait_until="domcontentloaded")
        try:
            me = self._call("GET", "/api/v2/users/me.json")["user"]
        except ZendeskError as e:
            raise ZendeskError(f"Couldn't reach Zendesk as a signed-in agent ({e}). Run: python -m triage_agent login") from e
        if not me.get("id") or me.get("role") == "end-user":
            raise ZendeskError("Not signed in to Zendesk as an agent. Run: python -m triage_agent login")
        self.me = me
        self.csrf = me.get("authenticity_token")
        return me

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        for _ in range(5):
            r = self.page.evaluate(_FETCH_JS, {"method": method, "path": path, "body": body, "csrf": self.csrf})
            if r["status"] == 429:
                time.sleep(min(int(r.get("retryAfter") or 10), 120))
                continue
            if r["status"] >= 400:
                raise ZendeskError(f"{method} {path} -> HTTP {r['status']}: {r['text'][:300]}")
            return json.loads(r["text"]) if r["text"] else {}
        raise ZendeskError(f"{method} {path}: still rate limited after retries")

    # --- reads ---

    def search_tickets(self, query: str, limit: int) -> list[dict]:
        q = urllib.parse.urlencode(
            {"query": query, "sort_by": "updated_at", "sort_order": "desc", "per_page": min(limit, 100)}
        )
        return self._call("GET", f"/api/v2/search.json?{q}")["results"][:limit]

    def get_ticket(self, ticket_id: int) -> dict:
        return self._call("GET", f"/api/v2/tickets/{ticket_id}.json")["ticket"]

    def comments(self, ticket_id: int, limit: int = 20) -> tuple[list[dict], dict[int, dict]]:
        """Most recent comments (oldest first) plus the users who wrote them."""
        data = self._call(
            "GET", f"/api/v2/tickets/{ticket_id}/comments.json?sort_order=desc&per_page={limit}&include=users"
        )
        users = {u["id"]: u for u in data.get("users", [])}
        return list(reversed(data["comments"])), users

    def groups(self) -> dict[str, int]:
        if self._groups is None:
            data = self._call("GET", "/api/v2/groups/assignable.json?per_page=100")
            self._groups = {g["name"]: g["id"] for g in data["groups"]}
        return self._groups

    def ticket_fields(self) -> list[dict]:
        """Active ticket fields, including which ones Zendesk requires before a ticket can be solved."""
        if self._fields is None:
            data = self._call("GET", "/api/v2/ticket_fields.json?per_page=100")
            self._fields = [f for f in data["ticket_fields"] if f.get("active", True)]
        return self._fields

    def find_users(self, query: str) -> list[dict]:
        q = urllib.parse.urlencode({"query": query})
        return self._call("GET", f"/api/v2/users/search.json?{q}")["users"][:10]

    def raw(self, method: str, path: str, body: dict | None = None) -> dict:
        if not path.startswith("/api/v2/"):
            raise ZendeskError("Only /api/v2/ paths are allowed")
        return self._call(method, path, body)

    # --- writes ---

    def update(self, ticket_id: int, changes: dict) -> dict:
        return self._call("PUT", f"/api/v2/tickets/{ticket_id}.json", {"ticket": changes})["ticket"]

    def note(self, ticket_id: int, body: str, public: bool = False, **changes) -> dict:
        return self.update(ticket_id, {"comment": {"body": body, "public": public}, **changes})
