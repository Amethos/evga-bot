# Zendesk triage agent

Runs on your Windows PC and works your Zendesk queue for you:

- **Does what you tell it, in plain language:** "close the Adobe ticket", "close all my tickets",
  "escalate the one from Sarah to Tier 2", "give sam@corp.com Send As on finance@". Type it in the
  chat window, or leave an internal note starting with `#agent` on the ticket.
- **Never talks to customers on its own.** It drafts a reply only when you ask, and sends one only
  when you explicitly tell it to (after you type `y`).
- **Triage:** sets category tags, priority and group on new activity, using your rules.
- **Closing:** solves tickets that are clearly done (resolved, duplicate, spam), and only when it's confident.
- **Exchange permissions:** reads access requests (Full Access, Send As, Send on Behalf, calendar,
  distribution groups), queues each one for your approval, then makes the change in the
  Exchange admin center web page, checks it took effect, and updates the ticket.
- **Learns your rules:** tell it things in plain language; it writes them up as rules, checks
  them against existing ones, and saves them only after you confirm.

It doesn't need the Zendesk API or Exchange PowerShell. It works through a browser that's
signed in as you, so it can only do what your own accounts can do.

> **Check first:** the agent acts as you in Zendesk and the Exchange admin center. Make sure
> your Zendesk admin / IT are fine with you automating your own account.

## Background triage: what it does on its own vs. what waits for you

| Action | Without asking | Waits for you |
|---|---|---|
| Tags, priority, group | ✓ | |
| Internal note explaining each decision | ✓ | |
| Solve a clearly finished ticket (confidence ≥ 0.85) | ✓ | |
| Solve when less sure | | tagged `agent_suggests_close` |
| Anything sensitive or unclear | | tagged `agent_needs_human` |
| Exchange permission changes | | every one, via `review` |
| New rules | | every one, confirmed by you |
| Replies to customers | never | only when you ask: drafts go in as internal notes, sending needs a typed `y` |
| Required fields (e.g. Category) | filled in when it closes a ticket | if it can't tell, it suggests closing instead |

Everything it does is written to `data/audit.jsonl`, and each decision is noted on the ticket.

## Setup (Windows)

You need Python 3.11+ (from python.org; tick "Add to PATH") and an Anthropic API key.

```powershell
git clone https://github.com/Amethos/zendesk-triage-agent.git
cd zendesk-triage-agent
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
notepad config.yaml          # set zendesk_subdomain
setx ANTHROPIC_API_KEY "sk-ant-..."   # then open a new terminal
```

It uses Microsoft Edge (already on Windows), so there's no browser to install.

Sign in once. The login is saved in `.browser-profile\`:

```powershell
python -m triage_agent login
```

Sign in to Zendesk and the Exchange admin center in the window that opens. Tick "stay signed in".

## Try it safely

`config.yaml` starts with `dry_run: true`: the agent decides and logs everything but changes nothing.

```powershell
python -m triage_agent once      # one pass over the queue
python -m triage_agent status    # what it decided
python -m triage_agent review    # look at queued permission requests
```

In dry run, approving a permission change runs a **rehearsal**: it finds the mailbox and user in
the admin center but is blocked from pressing anything that saves. A screenshot of where it
stopped is saved in `data\screens\`.

When its decisions look right, set `dry_run: false`.

## Telling it what to do

**Chat** is the main way to use it. It also keeps triaging in the background between your messages:

```powershell
python -m triage_agent chat
```

Talk to it the way you'd talk to a colleague. It works out which ticket you mean from your
queue (subject, description, requester, fields), and asks if more than one fits.

```
you> close the adobe ticket
#4512 Acrobat keeps crashing: close (solved), Category = Software::Adobe  (reinstall fixed it)
  OK? [Y/n]
agent> Closed #4512 as Software::Adobe.

you> close all my tickets
6 tickets to change:
  #4512 Acrobat keeps crashing: close (solved), Category = Software::Adobe  (reinstall fixed it)
  #4520 Printer jam 3rd floor: close (solved), Category = Hardware::Printer  (fixed on site)
  #4533 New starter laptop: close (solved), Category = Hardware::Laptop, Type = task
  ...
  #4541 VPN drops: close (solved), Category = Network::VPN  (⚠ customer asked a question yesterday)
  OK? [Y/n]
agent> Closed 6 tickets.

you> escalate the one from Sarah to Tier 2, urgent, and give it to Dana
you> draft a reply on the printer one saying a tech is on the way
agent> Drafted it as an internal note on #4520. Not sent.
you> what's open from acme.com older than a week?
you> from now on, password resets go to the Service Desk group
  (shows the rule and asks you to confirm)
```

**Closing tickets:** it fills in whatever Zendesk requires before a ticket can be solved
(Category and any other required fields), choosing from the allowed options based on what the
ticket says. It won't try to solve a ticket with a required field still empty.

**Batches:** "close all my tickets" and similar requests show the whole plan and need one
confirmation. "My tickets" means `my_queue_query` in `config.yaml` (by default, open tickets
assigned to you).

It can do anything your Zendesk login can do, including merging tickets, applying macros and
changing organizations. Things it doesn't have a specific tool for go through the Zendesk API
directly.

- Changes are shown first. Press **Enter to confirm**, or set `confirm_writes: false` in
  `config.yaml` to skip this.
- **Always asked for, even with `confirm_writes: false`:** anything a customer would see, anything
  that deletes, and Exchange permission changes.
- **Customers:** it doesn't reply or draft unless you ask. Drafts are internal notes starting
  "DRAFT REPLY (not sent)". From `#agent` notes it can only draft, never send.
  Note that your Zendesk may have its own triggers that email the customer when a ticket is
  solved. That's your Zendesk configuration, not the agent.

Chat commands: `/review`, `/status`, `/pause` (background triage on/off), `/new` (fresh
conversation), `/quit`.

**`#agent` notes** let you give instructions without leaving Zendesk. Add an internal note
to the ticket:

```
#agent escalate this to Tier 2, urgent, and assign it to dana@corp.com
```

On its next pass (within `poll_seconds`) the agent carries it out and replies with an internal
note saying what it did. You wrote the instruction, so it doesn't ask again. Only internal notes
written by you count, never customers' or other agents'.

## Day to day

Either keep `chat` open, or run it with no window:

```powershell
python -m triage_agent run
```

The windowless `run` still triages, follows `#agent` notes and makes approved changes.

To start it automatically when you log in, create a scheduled task. Run this once from the
project folder in **Command Prompt** (not PowerShell):

```bat
schtasks /Create /SC ONLOGON /TN "Zendesk triage agent" /TR "\"%CD%\run_agent.cmd\""
```

Output goes to `data\agent.log`. Only one copy can use the browser profile at a time. Stop the
background one (Task Manager → `pythonw.exe`) before running `login` or `once`.

**Approving permission changes the agent found in tickets:** run `python -m triage_agent review`
(or `/review` in chat). For each request you see
the ticket, the exact change and the agent's reasoning, then approve or reject it. The background
agent makes approved changes on its next pass. A rejection is noted on the ticket, and you're
offered the chance to turn your reason into a rule.

## Teaching it rules

There are several ways, and all of them show you the rule before saving it:

- In chat, say it as a standing instruction: "from now on…", "always…", "never…".
- From the command line:

```powershell
python -m triage_agent teach "Billing tickets from enterprise customers are always urgent"
```

- Leave an **internal note** on any ticket starting with `#rule`, e.g.
  `#rule never close a ticket if the customer replied in the last 24 hours`.
  It shows up in `review` for you to confirm.
- When you **reject** a permission request, say why. You'll be asked if it should become a rule.

When a new rule conflicts with an old one, it asks which wins. A rule that would grant access with
less review than before gets a warning and needs you to type `yes`.

Rules live in `rules.yaml`, which you can read and edit by hand. Each change is committed to git,
so `git log rules.yaml` shows the history and `git revert` undoes one.

```powershell
python -m triage_agent rules               # list
python -m triage_agent rules disable r-003
```

## Settings worth knowing

| Setting | Default | |
|---|---|---|
| `ticket_query` | `type:ticket status<solved` | Any Zendesk search, e.g. limit to one group |
| `poll_seconds` | 60 | How often to check for new activity |
| `auto_close_min_confidence` | 0.85 | Below this it suggests closing instead |
| `my_queue_query` | `type:ticket assignee:me status<solved` | What "my tickets" means |
| `confirm_writes` | true | Chat: confirm each change with Enter |
| `headless` | true | false = watch it work in a visible window |
| `models.*` | Claude Opus 5.5 throughout: low effort for the first triage pass and for your instructions, high effort for access requests and unsure tickets | |

## How it works

- `zendesk.py`: the Zendesk agent interface loads data from `/api/v2` using your login.
  The agent makes the same requests from inside the signed-in tab, which is much faster and
  steadier than clicking through pages.
- `decide.py`: one structured Claude call per ticket with your rules; uncertain or
  access-related tickets get a second, more careful look.
- `exchange.py`: a small browser agent that reads the admin center's accessibility tree
  and clicks/types to make one approved change. Code-level guard rails: Microsoft admin pages
  only, no destructive buttons when granting, no saving in rehearsal, 40-step limit.
- `command.py`: follows your instructions from chat or `#agent` notes, using tools to look at
  your queue, search, read and update tickets (one or many at once), look up users, call any
  Zendesk endpoint, make Exchange changes and save rules.
- `fields.py`: knows your ticket fields: which are required before solving, their allowed
  options, and how to turn "Category: Adobe" into what Zendesk stores.
- `rules.py`: turns what you say into rules and keeps `rules.yaml`.
- `agent.py`: the loop. It only re-reads a ticket when it has changed since the last look.

## Tests

```powershell
pip install pytest
playwright install chromium   # the tests use Playwright's own Chromium
python -m pytest tests
```

The tests run a fake Zendesk and a fake admin center page locally in a real headless browser,
with Claude's answers scripted, so they need no API key or accounts.
