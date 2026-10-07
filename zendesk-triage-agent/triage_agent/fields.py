"""Zendesk ticket fields: what a ticket has filled in, what it still needs before it can be
solved, and turning names like "Category: Software::Adobe" into the values Zendesk stores."""

SYSTEM_TYPES = {"subject", "description", "status", "custom_status", "priority", "tickettype", "group", "assignee"}
MAX_OPTIONS_SHOWN = 80


def _empty(value) -> bool:
    return value in (None, "", [], False)


def custom_fields(zd) -> list[dict]:
    return [f for f in zd.ticket_fields() if f["type"] not in SYSTEM_TYPES]


def _options(field) -> list[dict]:
    return field.get("custom_field_options") or []


def current(ticket: dict, field: dict):
    for cf in ticket.get("custom_fields") or []:
        if cf["id"] == field["id"]:
            return cf["value"]
    return None


def display(field: dict, value):
    if _empty(value):
        return None
    names = {o["value"]: o["name"] for o in _options(field)}
    if isinstance(value, list):
        return "; ".join(names.get(v, str(v)) for v in value)
    return names.get(value, value)


def missing_to_solve(zd, ticket: dict, pending: dict | None = None) -> list[str]:
    """Titles of fields Zendesk requires before solving that are still empty.
    pending: changes about to be sent ({"custom_fields": [...], "type": ..., ...})."""
    pending = pending or {}
    pending_custom = {cf["id"]: cf["value"] for cf in pending.get("custom_fields", [])}
    missing = []
    for f in zd.ticket_fields():
        if not f.get("required"):
            continue
        if f["type"] in SYSTEM_TYPES:
            key = {"tickettype": "type", "priority": "priority", "group": "group_id"}.get(f["type"])
            if key and _empty(pending.get(key, ticket.get(key))):
                missing.append(f["title"])
            continue
        value = pending_custom.get(f["id"], current(ticket, f))
        if _empty(value):
            missing.append(f["title"])
    return missing


def options_text(field: dict) -> str:
    names = [o["name"] for o in _options(field)]
    if not names:
        return ""
    shown = " | ".join(names[:MAX_OPTIONS_SHOWN])
    return shown + (f" | ... ({len(names) - MAX_OPTIONS_SHOWN} more)" if len(names) > MAX_OPTIONS_SHOWN else "")


def describe(zd, ticket: dict) -> str:
    """The ticket's custom fields for the model: filled values, and required-to-solve fields with options."""
    lines = []
    missing = set(missing_to_solve(zd, ticket))
    for f in custom_fields(zd):
        value = display(f, current(ticket, f))
        if value is None and not f.get("required"):
            continue
        line = f"- {f['title']}: {value if value is not None else '(empty)'}"
        if f.get("required"):
            line += "  [required to solve]"
        if f["title"] in missing and _options(f):
            line += f"\n    options: {options_text(f)}"
        lines.append(line)
    for title in sorted(missing - {f["title"] for f in custom_fields(zd)}):
        lines.append(f"- {title}: (empty)  [required to solve]")
    return "\n".join(lines) or "(no custom fields)"


def filled(zd, ticket: dict) -> dict[str, str]:
    return {f["title"]: v for f in custom_fields(zd) if (v := display(f, current(ticket, f))) is not None}


def find_field(zd, name: str) -> dict:
    key = name.strip().lower()
    for f in custom_fields(zd):
        if key in (f["title"].lower(), (f.get("title_in_portal") or "").lower()):
            return f
    raise ValueError(f"No ticket field named '{name}'. Fields: {', '.join(f['title'] for f in custom_fields(zd))}")


def _match_option(field: dict, wanted: str) -> str:
    """Option value for a name. Accepts the full name ("Software::Adobe") or, for nested
    dropdowns, just the last part ("Adobe") when that's unique."""
    w = wanted.strip().lower()
    opts = _options(field)
    for o in opts:
        if w in (o["name"].lower(), str(o["value"]).lower()):
            return o["value"]
    leaf = [o for o in opts if o["name"].split("::")[-1].strip().lower() == w]
    if len(leaf) == 1:
        return leaf[0]["value"]
    raise ValueError(f"'{wanted}' isn't an option for {field['title']}. Options: {options_text(field)}")


def resolve(zd, name: str, value: str) -> dict:
    """{"id": ..., "value": ...} for Zendesk's custom_fields, from a field name and a readable value."""
    f = find_field(zd, name)
    kind = f["type"]
    if kind == "tagger":
        raw = _match_option(f, value)
    elif kind == "multiselect":
        raw = [_match_option(f, part) for part in value.split(";") if part.strip()]
    elif kind == "checkbox":
        raw = value.strip().lower() in ("true", "yes", "y", "1", "checked")
    elif kind == "integer":
        raw = int(value)
    elif kind == "decimal":
        raw = float(value)
    else:
        raw = value
    return {"id": f["id"], "value": raw}
