"""Watch a tmux pane and stream pair-programming suggestions from Claude.

Run `claude-pair` inside tmux: it splits off a side pane and watches the pane
you were in. The watcher polls `tmux capture-pane` (which sees partial,
un-executed command lines), debounces changes, and asks Claude for a
suggestion. The model answers SKIP for anything not worth interrupting for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule

DEFAULT_MODEL = "claude-opus-5"

CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-pair"
)
VIM_STATE_FILE = CACHE_DIR / "vim_state.json"
VIM_RECENT_FILE = CACHE_DIR / "vim_recent.json"  # ring of recent vim files
SHELL_EVENT_FILE = CACHE_DIR / "shell_event"  # fish hook: command finished
EDITOR_EVENT_FILE = CACHE_DIR / "editor_event"  # vim hook: file saved
VIM_STATE_MAX_AGE = 120  # seconds before vim state is considered stale

# redact obvious secret material from anything sent to the API: the pane
# can show `cat .env` output, tokens in error traces, pasted creds, etc.
SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:ant|proj|live|test)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    # KEY=value / key: "value" assignments where the name screams secret
    re.compile(
        r"""(?ix)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_?KEY|PRIVATE_?KEY)
            [A-Z0-9_]*\s*[=:]\s*)(['"]?)[^\s'"]{8,}\2"""
    ),
]


def scrub_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


# never auto-load files whose names suggest secrets; explicit
# `claude-pair context add` remains available for deliberate loading
SENSITIVE_NAME_RE = re.compile(
    r"(^\.env($|\.)|secret|credential|token|password|\.pem$|\.key$"
    r"|id_rsa|id_ed25519|id_ecdsa|\.netrc$|_history$|\.kdbx$)",
    re.IGNORECASE,
)
INBOX_DIR = CACHE_DIR / "inbox"  # `claude-pair say` drops messages here
LAST_SUGGESTION_FILE = CACHE_DIR / "last_suggestion.txt"
LAST_CODE_FILE = CACHE_DIR / "last_code.txt"  # just the fenced code blocks
LAST_DIFF_FILE = CACHE_DIR / "last_diff.txt"  # just the ```diff fences
SUGGESTION_LOG = CACHE_DIR / "suggestions.log"
DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "claude-pair"
)
JOURNAL_FILE = DATA_DIR / "journal.md"  # running human-readable activity log
USAGE_FILE = DATA_DIR / "usage.jsonl"  # one line per API call, for `costs`
FEEDBACK_FILE = DATA_DIR / "feedback.jsonl"  # good/bad votes, for tuning

# $/MTok (input, output); cache write bills 1.25x input, cache read 0.1x.
# Estimates only — the Console is authoritative.
PRICING_PER_MTOK = {
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "claude-fable": (10.0, 50.0),
    "claude-mythos": (10.0, 50.0),
}

PANE_FILE = CACHE_DIR / "pane"  # the running watcher's own tmux pane id
HIDDEN_WINDOW = "_claude_pair"  # holding window for a hidden watcher pane
CONTEXT_DIR = CACHE_DIR / "context"  # loaded reference files (content snapshots)
DEFAULT_CONTEXT_BUDGET = 120_000  # chars per load (~30k tokens)
CONTEXT_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".tox", ".ruff_cache", "dist", "build", ".idea",
    ".ipynb_checkpoints", ".egg-info",
}

SYSTEM_PROMPT = """\
You are an expert pair programmer quietly looking over the user's shoulder. \
Each user message is a snapshot of their terminal pane (and, when they are in \
vim, the region of the file around their cursor, including unsaved edits). \
Snapshots arrive whenever the screen changes and then goes briefly quiet — so \
you often see half-typed commands and code mid-edit. A <last_command> block, \
when present, is a precise signal from the user's shell: that exact command \
just finished with that exit status and duration. A nonzero status usually \
deserves a suggestion; a routine success usually doesn't. The watcher follows the \
user's active tmux pane, so consecutive snapshots may come from different \
panes or windows — the pane id is in the <terminal> tag. A pane switch is \
not itself worth commenting on.

The user works in fish shell and vim on Linux. Tailor suggestions accordingly \
(fish syntax, not bash; vim-native ways of doing things).

A <reference_context> block, when present, holds files or code the user \
loaded for you to consult — background knowledge for understanding what \
they're doing, not something to review or comment on line by line. Use it to \
inform your suggestions about the pane and editor. A <vim_recent_files> \
block is similar but automatic: the full saved contents of files they \
recently edited in vim. Where a snapshot's <vim> cursor block disagrees \
with it, the cursor block shows unsaved edits and wins.

Respond with exactly SKIP unless you have something genuinely worth \
interrupting for, such as:
- a typo or bug in a command they are still typing, before they run it
- a command that just failed, with the likely fix
- a destructive or dangerous command about to be run
- a real bug, or a clearly better approach, in code visible in the editor
- a meaningfully faster way to do what they are obviously trying to do
- a quick implementation of code they are trying to write

Occasionally a <journal_request> asks you to write the next entry of the \
user's work journal. Respond with ONLY the entry text: one entry of 1-2 \
sentences, high level — what they worked on in that stretch, where they \
left off, what seems next. Past tense, their perspective, plain prose, no \
bullets, no SKIP. Think "worked on the parser refactor; got the tokenizer \
tests green, left off mid-rewrite of parse_expr" — not a list of commands.
A <journal_recent> block, when present, is the tail of that journal — it \
appears when you may lack context (session start, after a break); use it to \
understand what the user has been doing.

A <returned after_minutes=N> marker means the user just came back from a \
break. Do not SKIP that snapshot: give a brief re-grounding instead — 1) \
what they were working on before the break (use the earlier snapshots in \
this conversation), 2) the state they left it in (done, failing, half-typed), \
3) a suggested next step. A few short lines; write it as a re-grounding for \
someone who has lost their mental context, not a continuation. If you have \
no pre-break snapshots to draw on, just say you're watching again and skip \
the recap.

A <user_message> starting with "FEEDBACK good" or "FEEDBACK bad" is \
meta-feedback about your recent suggestions, not a question. Take it on \
board for the rest of the session — recalibrate what you surface and how — \
and reply with just SKIP. Never re-answer the original topic, never defend \
or explain yourself.

The user can also talk to you directly:
- A <user_message> block in a snapshot is the user addressing you. Always \
answer it — never SKIP a snapshot that contains one. Be concise but complete; \
you may exceed the bullet limit for a real question.
- A shell comment addressed to you in the terminal (like \
`# claude: how do I undo the last commit?`) is also a direct question. Answer \
it the first time you see it; if an earlier reply of yours already answered \
it, SKIP.

Rules:
- Most snapshots deserve SKIP. Routine, correct activity needs no comment. \
Half-finished work is not a problem to fix; only speak if the part already \
written is wrong or headed somewhere bad.
- Never repeat or rephrase a suggestion you already made (your earlier replies \
are in this conversation). If the situation hasn't changed, SKIP.
- Don't guess at intent you can't see. If a command is ambiguous but \
plausible, SKIP.
- Respond with only the suggestion or SKIP — no preamble, no explanation of \
your reasoning, no "let me look". Get straight to it.
- When you do speak: at most 3 short bullets ("- "), most important first. \
Simple markdown only: bullets, `inline code`, and fenced code blocks with a \
language tag (```fish, ```python, ```vim) for commands, snippets, and \
implementations. No headers, no tables, no bold walls of text. Keep prose \
lines under ~52 characters where possible — the output pane is narrow. A \
one-line command fix can just be a fenced command.
- Fences are the deliverable: anything the user might paste into their \
editor or run goes inside a fenced block, ready to use as-is; explanation \
stays outside. The user has commands that insert your latest fenced code \
directly at their cursor, so never put prose, placeholders you haven't \
flagged, or "..." elisions inside a fence.
- To change an existing file (usually one visible in <vim> or \
<vim_recent_files>), use a ```diff fence instead: a unified diff against \
the saved file with `--- a/<path>` and `+++ b/<path>` headers and correct \
hunk line numbers. The user applies it in vim with one keystroke \
(:ClaudeApply), so it must apply cleanly. New standalone code still goes \
in language fences.
"""


# ---------------------------------------------------------------------------
# tmux + vim context gathering


def capture_pane(target: str, scrollback: int) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", f"-{scrollback}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux capture-pane failed")
    return result.stdout.rstrip()


# ---------------------------------------------------------------------------
# usage accounting: log every call's token usage for `claude-pair costs`


def estimate_cost_usd(model: str, rec: dict) -> float:
    for prefix, (in_rate, out_rate) in PRICING_PER_MTOK.items():
        if model.startswith(prefix):
            break
    else:
        in_rate, out_rate = 5.0, 25.0
    return (
        rec["in"] * in_rate
        + rec["cache_w"] * in_rate * 1.25
        + rec["cache_r"] * in_rate * 0.10
        + rec["out"] * out_rate
    ) / 1e6


def classify_reply(reply: str, direct: bool) -> str:
    """Cheap local label: skip / tip / answer, tagged with fence languages."""
    if not reply or reply.strip().upper() == "SKIP":
        return "skip"
    langs = sorted({m.lower() for m in re.findall(r"```(\w+)", reply)})
    base = "answer" if direct else "tip"
    return f"{base}:{'+'.join(langs)}" if langs else base


def log_usage(kind: str, model: str, usage, extra: dict | None = None) -> None:
    """Append one line to usage.jsonl. Best-effort, no API cost."""
    try:
        rec = {
            "ts": int(time.time()),
            "kind": kind,
            "model": model,
            "in": getattr(usage, "input_tokens", 0) or 0,
            "out": getattr(usage, "output_tokens", 0) or 0,
            "cache_w": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_r": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
        rec.update(extra or {})
        rec["usd"] = round(estimate_cost_usd(model, rec), 6)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with USAGE_FILE.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def costs_cmd(argv: list[str]) -> None:
    """`claude-pair costs [DAYS]` — spend breakdown from usage.jsonl."""
    days = float(argv[0]) if argv and argv[0].replace(".", "").isdigit() else 7.0
    cutoff = time.time() - days * 86400
    records = []
    try:
        with USAGE_FILE.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts", 0) >= cutoff:
                    records.append(rec)
    except OSError:
        sys.exit(f"claude-pair: no usage log yet (will appear at {USAGE_FILE})")
    console = Console(highlight=False)
    if not records:
        console.print(f"no calls in the last {days:g} days", style="dim")
        return

    def agg(keyfn, by_key=False):
        out: dict[str, list[float]] = {}
        for r in records:
            k = keyfn(r)
            entry = out.setdefault(k, [0, 0.0])
            entry[0] += 1
            entry[1] += r.get("usd", 0.0)
        key = (lambda kv: kv[0]) if by_key else (lambda kv: -kv[1][1])
        return sorted(out.items(), key=key)

    total_usd = sum(r.get("usd", 0.0) for r in records)
    total_in = sum(r["in"] + r["cache_w"] + r["cache_r"] for r in records)
    cached = sum(r["cache_r"] for r in records)
    console.print(
        f"[bold]claude-pair costs — last {days:g} days[/] "
        f"[dim](estimates; Console is authoritative)[/]"
    )
    console.print(
        f"{len(records)} calls · est [bold]${total_usd:.2f}[/] · "
        f"{total_in:,} input tokens ({cached / max(1, total_in):.0%} cache reads) · "
        f"{sum(r['out'] for r in records):,} output tokens"
    )

    from rich.table import Table

    for title, keyfn, by_key in (
        ("by call kind", lambda r: r.get("kind", "?"), False),
        ("by reply", lambda r: r.get("class", "—"), False),
        ("by day", lambda r: datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d"), True),
    ):
        table = Table(title=title, title_style="dim", title_justify="left",
                      show_edge=False, pad_edge=False)
        table.add_column("")
        table.add_column("calls", justify="right")
        table.add_column("est $", justify="right")
        table.add_column("", justify="left", style="dim")
        for key, (n, usd) in agg(keyfn, by_key):
            share = usd / total_usd if total_usd else 0
            table.add_row(key, str(n), f"{usd:.2f}", "▪" * int(share * 20))
        console.print()
        console.print(table)


# ---------------------------------------------------------------------------
# running journal: NOTE lines the model emits about completed activity


def journal_append(note: str) -> None:
    """Append one entry, adding a date header when the day changes."""
    if not note:
        return
    try:
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            header_needed = f"## {today}" not in JOURNAL_FILE.read_text()[-4096:]
        except OSError:
            header_needed = True
        with JOURNAL_FILE.open("a") as f:
            if header_needed:
                f.write(f"\n## {today}\n\n")
            f.write(f"- {datetime.now():%H:%M} {note.strip()}\n")
    except OSError:
        pass  # journaling is best-effort; never break the watcher


def journal_tail(lines: int = 30) -> str:
    try:
        content = JOURNAL_FILE.read_text()
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:]).strip()


def journal_age_minutes() -> float | None:
    """Minutes since the last journal entry (file mtime), or None if none."""
    try:
        return (time.time() - JOURNAL_FILE.stat().st_mtime) / 60
    except OSError:
        return None


def journal_last_section(entries: int = 5) -> str:
    """Markdown of the last few journal entries, with their date header."""
    try:
        lines = JOURNAL_FILE.read_text().splitlines()
    except OSError:
        return ""
    count = 0
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("- "):
            count += 1
            if count >= entries:
                start = i
                break
    header = None
    for j in range(start, -1, -1):
        if lines[j].startswith("## "):
            header = lines[j]
            break
    picked = lines[start:]
    if header is not None and header not in picked:
        picked = [header, ""] + picked
    return "\n".join(picked).strip()


def _ago(minutes: float) -> str:
    if minutes < 90:
        return f"{int(minutes)} min ago"
    if minutes < 36 * 60:
        return f"{minutes / 60:.0f} hours ago"
    return f"{minutes / 1440:.0f} days ago"


def journal_cmd(argv: list[str]) -> None:
    """`claude-pair journal [N | rollup]` — show the tail, or compress old weeks."""
    if argv and argv[0] == "rollup":
        rollup_journal()
        return
    n = int(argv[0]) if argv and argv[0].isdigit() else 25
    console = Console(highlight=False)
    try:
        lines = JOURNAL_FILE.read_text().splitlines()
    except OSError:
        sys.exit(f"claude-pair: no journal yet (will appear at {JOURNAL_FILE})")
    console.print(str(JOURNAL_FILE), style="dim")
    console.print(Markdown("\n".join(lines[-n:])))


def _oneshot_client():
    import anthropic

    return anthropic, anthropic.Anthropic(), os.environ.get(
        "CLAUDE_PAIR_MODEL", DEFAULT_MODEL
    )


def standup_cmd(argv: list[str]) -> None:
    """`claude-pair standup [DAYS]` — a standup update from the journal."""
    days = float(argv[0]) if argv and argv[0].replace(".", "").isdigit() else 3.0
    try:
        tail = "\n".join(JOURNAL_FILE.read_text().splitlines()[-200:]).strip()
    except OSError:
        sys.exit(f"claude-pair: no journal yet (will appear at {JOURNAL_FILE})")
    if not tail:
        sys.exit("claude-pair: journal is empty")
    anthropic, client, model = _oneshot_client()
    today = datetime.now().strftime("%A %Y-%m-%d")
    request = (
        f"Today is {today}. From the work journal below, write a standup "
        f"update covering roughly the last {days:g} day(s) of entries: what "
        "got done, what's in progress and where it left off, and any blockers "
        "implied. 3-6 short markdown bullets, no headers, no preamble.\n\n"
        f"{tail}"
    )
    console = Console(highlight=False)
    try:
        with console.status("[dim]summarizing…[/]"):
            response = client.messages.create(
                model=model, max_tokens=600,
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": request}],
            )
    except (anthropic.APIError, anthropic.APIConnectionError) as exc:
        sys.exit(f"claude-pair: standup call failed ({exc.__class__.__name__})")
    log_usage("standup", model, response.usage)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    console.print(Markdown(text))


def rollup_journal() -> None:
    """Compress journal entries older than 7 days into per-week summaries."""
    try:
        original = JOURNAL_FILE.read_text()
    except OSError:
        sys.exit(f"claude-pair: no journal yet (will appear at {JOURNAL_FILE})")

    sections: list[tuple[str | None, list[str]]] = []
    header: str | None = None
    body: list[str] = []
    for line in original.splitlines():
        if line.startswith("## "):
            sections.append((header, body))
            header, body = line, []
        else:
            body.append(line)
    sections.append((header, body))

    cutoff = datetime.now().date() - timedelta(days=7)
    plan: list[tuple[str, object]] = []  # ("week", key) | ("raw", section)
    weeks: dict[tuple, dict] = {}
    for head, lines in sections:
        day = None
        if head:
            try:
                day = datetime.strptime(head[3:].strip(), "%Y-%m-%d").date()
            except ValueError:
                day = None
        if day and day < cutoff:
            key = day.isocalendar()[:2]
            if key not in weeks:
                weeks[key] = {"start": day - timedelta(days=day.weekday()),
                              "chunks": []}
                plan.append(("week", key))
            weeks[key]["chunks"].append(head + "\n" + "\n".join(lines))
        else:
            plan.append(("raw", (head, lines)))

    console = Console(highlight=False)
    if not weeks:
        console.print("nothing older than 7 days to roll up", style="dim")
        return

    anthropic, client, model = _oneshot_client()
    summaries: dict[tuple, str] = {}
    for key, info in weeks.items():
        request = (
            "Condense this week's work-journal entries into 2-4 high-level "
            "bullet lines. Keep anything still relevant later: decisions, "
            "outcomes, left-off states. Output only the bullets.\n\n"
            + "\n\n".join(info["chunks"])
        )
        try:
            with console.status(f"[dim]rolling up week of {info['start']}…[/]"):
                response = client.messages.create(
                    model=model, max_tokens=400,
                    thinking={"type": "disabled"},
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": request}],
                )
        except (anthropic.APIError, anthropic.APIConnectionError) as exc:
            sys.exit(
                f"claude-pair: rollup call failed ({exc.__class__.__name__}); "
                "journal unchanged"
            )
        log_usage("rollup", model, response.usage)
        summaries[key] = "".join(
            b.text for b in response.content if b.type == "text"
        ).strip()

    backup = JOURNAL_FILE.with_suffix(".md.bak")
    backup.write_text(original)
    parts: list[str] = []
    for kind, value in plan:
        if kind == "week":
            info = weeks[value]
            parts.append(
                f"## Week of {info['start']} (rollup)\n\n{summaries[value]}\n"
            )
        else:
            head, lines = value
            segment = "\n".join(([head] if head else []) + lines).strip("\n")
            if segment.strip():
                parts.append(segment + "\n")
    JOURNAL_FILE.write_text("\n" + "\n".join(parts))
    console.print(
        f"rolled up {len(weeks)} week(s); previous journal saved to {backup}"
    )


# ---------------------------------------------------------------------------
# reference context: files/dirs the user loads for Claude to consult


def _read_text_file(path: Path) -> str | None:
    """File contents as text, or None if it's binary/unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:  # NUL byte → treat as binary
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def gather_context(path: Path, budget: int) -> tuple[str, list[str]]:
    """Concatenate a file or directory's text within a char budget.

    Returns (text, notes) — notes describe what was skipped, so nothing is
    silently dropped.
    """
    if path.is_dir():
        files: list[Path] = []
        for root, dirs, names in os.walk(path):
            dirs[:] = sorted(
                d for d in dirs
                if d not in CONTEXT_SKIP_DIRS and not d.startswith(".")
            )
            files.extend(sorted(Path(root) / n for n in names))
    else:
        files = [path]

    parts, used, skipped_binary, skipped_budget = [], 0, 0, []
    for f in files:
        text = _read_text_file(f)
        if text is None:
            skipped_binary += 1
            continue
        chunk = f"=== {f} ===\n{text}\n"
        if used + len(chunk) > budget:
            skipped_budget.append(str(f))
            continue
        parts.append(chunk)
        used += len(chunk)

    notes = []
    if skipped_binary:
        notes.append(f"skipped {skipped_binary} binary/unreadable file(s)")
    if skipped_budget:
        notes.append(
            f"budget ({budget} chars) hit: skipped {len(skipped_budget)} "
            f"file(s), e.g. {skipped_budget[0]}"
        )
    return "\n".join(parts), notes


def _context_slug(source: str) -> str:
    """Deterministic filename for a source, so re-adding it replaces it."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", source)[-120:] + ".ctx"


def add_context_paths(paths: list[str], budget: int) -> list[str]:
    """Snapshot each path's text into the context store. Returns status lines."""
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            out.append(f"no such path: {raw}")
            continue
        p = p.resolve()
        text, notes = gather_context(p, budget)
        if not text.strip():
            out.append(f"nothing readable in {raw}")
            continue
        dest = CONTEXT_DIR / _context_slug(str(p))
        dest.write_text(f"# source: {p}\n{text}")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        out.append(f"loaded {raw} [{len(text)} chars]{suffix}")
    return out


def load_context_text() -> str:
    """The combined <reference_context> block from the store (empty if none)."""
    try:
        items = sorted(CONTEXT_DIR.glob("*.ctx"))
    except OSError:
        return ""
    blocks = []
    for f in items:
        try:
            blocks.append(f.read_text())
        except OSError:
            continue
    if not blocks:
        return ""
    return "<reference_context>\n" + "\n\n".join(blocks) + "\n</reference_context>"


def context_signature() -> tuple:
    """Cheap fingerprint of the store, to detect changes without re-reading."""
    try:
        items = sorted(CONTEXT_DIR.glob("*.ctx"))
    except OSError:
        return ()
    sig = []
    for f in items:
        try:
            st = f.stat()
            sig.append((f.name, int(st.st_mtime), st.st_size))
        except OSError:
            continue
    return tuple(sig)


def _window_visible(pane: str) -> bool:
    """True if `pane`'s window is the one the user is currently viewing."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{window_active}"],
        capture_output=True,
        text=True,
    )
    # if we can't tell, assume visible (don't ping spuriously)
    return result.returncode != 0 or result.stdout.strip() != "0"


def summarize(reply: str) -> str:
    """First meaningful line of a suggestion, for a one-line status ping."""
    for line in reply.splitlines():
        text = line.strip().lstrip("-*").strip().strip("`").strip()
        if text:
            return text[:64]
    return "new suggestion"


def _display_message(text: str, duration_ms: int = 4000) -> None:
    """Show a tmux status-line message ('#' doubled to defeat expansion)."""
    text = text.replace("#", "##")
    # -d sets duration (tmux >= 3.2); fall back to the user's display-time
    if subprocess.run(
        ["tmux", "display-message", "-d", str(duration_ms), text],
        capture_output=True,
    ).returncode != 0:
        subprocess.run(["tmux", "display-message", text], capture_output=True)


def notify_status(own_pane: str | None, summary: str) -> None:
    """Ping the tmux status line if the watcher's pane isn't on screen."""
    if not own_pane or _window_visible(own_pane):
        return
    _display_message("✻ claude-pair: " + summary + "  (:cl / claude-pair last)")


def client_activity_wall() -> float | None:
    """Epoch time of the user's last keypress in the attached client, if any."""
    r = _tmux("display-message", "-p", "#{client_activity}")
    value = r.stdout.strip()
    if r.returncode != 0 or not value.isdigit() or int(value) == 0:
        return None
    return float(value)


def resolve_active_pane(own_pane: str | None) -> str | None:
    """The active pane of the active window in this session, if it isn't us."""
    result = subprocess.run(
        ["tmux", "list-panes", "-s", "-F", "#{pane_id} #{pane_active} #{window_active}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "1" and parts[2] == "1":
            return parts[0] if parts[0] != own_pane else None
    return None


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def pane_cmd(argv: list[str], width: int = 60) -> None:
    """`claude-pair hide|show|toggle` — stash/restore the watcher pane.

    Typed in a shell, messages print normally. Run from a tmux binding
    (`bind h run-shell "claude-pair toggle"`), stdout must stay EMPTY —
    any run-shell output opens tmux's view-mode overlay that has to be
    dismissed — so messages go to the status line instead, and we exit 0
    even on errors (nonzero can pop the overlay too).
    """
    interactive = sys.stdout.isatty()

    def report(msg: str, fail: bool = False) -> None:
        if interactive:
            print(msg, file=sys.stderr if fail else sys.stdout)
            if fail:
                sys.exit(1)
        else:
            _display_message(msg, duration_ms=2000)
            if fail:
                sys.exit(0)  # deliberate: keep run-shell quiet

    action = argv[0] if argv else "toggle"
    if action not in ("hide", "show", "toggle"):
        report("usage: claude-pair [hide | show | toggle]", fail=True)
        return

    try:
        pane = PANE_FILE.read_text().strip()
    except OSError:
        pane = ""
    if not pane:
        report("claude-pair: no running watcher found", fail=True)
        return

    # a dead pane id makes tmux fall back to the current pane, so verify it
    # actually exists rather than trusting display-message's target
    live = _tmux("list-panes", "-a", "-F", "#{pane_id}")
    if pane not in live.stdout.split():
        PANE_FILE.unlink(missing_ok=True)
        report("claude-pair: the watcher pane is gone", fail=True)
        return

    info = _tmux("display-message", "-p", "-t", pane,
                 "#{window_name}\t#{window_panes}")
    if info.returncode != 0:
        report("claude-pair: the watcher pane is gone", fail=True)
        return
    window_name, _, panes = info.stdout.strip().partition("\t")
    hidden = window_name == HIDDEN_WINDOW

    if action == "toggle":
        action = "show" if hidden else "hide"

    if action == "hide":
        if hidden:
            report("claude-pair: already hidden")
            return
        if panes.strip() == "1":
            report("claude-pair: watcher is the only pane in its window; "
                   "nothing to reclaim by hiding", fail=True)
            return
        r = _tmux("break-pane", "-d", "-s", pane, "-n", HIDDEN_WINDOW)
        if r.returncode != 0:
            report(f"claude-pair: {r.stderr.strip()}", fail=True)
            return
        report("✻ claude-pair hidden (still running)")
    else:  # show
        if not hidden:
            report("claude-pair: already visible")
            return
        dst = os.environ.get("TMUX_PANE")
        cmd = ["join-pane", "-h", "-l", str(width), "-s", pane]
        if dst:
            cmd += ["-t", dst]
        r = _tmux(*cmd)
        if r.returncode != 0:
            report(f"claude-pair: {r.stderr.strip()}", fail=True)
            return
        report("✻ claude-pair shown")


def read_recent_vim_files(limit: int) -> list[Path]:
    try:
        paths = json.loads(VIM_RECENT_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(paths, list):
        return []
    return [Path(p) for p in paths[:limit] if isinstance(p, str)]


def vim_context_signature(limit: int) -> tuple:
    """Cheap per-loop fingerprint: rebuild only when a tracked file changes."""
    sig = []
    for p in read_recent_vim_files(limit):
        try:
            st = p.stat()
            sig.append((str(p), int(st.st_mtime), st.st_size))
        except OSError:
            continue
    return tuple(sig)


def build_vim_context(limit: int, budget: int) -> str:
    """Full saved contents of recently edited vim files, budget-guarded."""
    parts: list[str] = []
    used = 0
    skipped: list[str] = []
    for p in read_recent_vim_files(limit):
        if SENSITIVE_NAME_RE.search(p.name):
            continue  # never auto-load likely-secret files
        text = _read_text_file(p)
        if text is None:
            continue
        chunk = f"=== {p} ===\n{scrub_secrets(text)}\n"
        if used + len(chunk) > budget:
            skipped.append(p.name)
            continue
        parts.append(chunk)
        used += len(chunk)
    if not parts:
        return ""
    note = (
        f"\n(budget hit: {', '.join(skipped)} not included)" if skipped else ""
    )
    return (
        "<vim_recent_files>\nFull contents, as last saved, of files the user "
        "recently edited in vim (most recent first). Unsaved edits appear in "
        f"each snapshot's <vim> block instead.{note}\n\n"
        + "\n".join(parts)
        + "</vim_recent_files>"
    )


def event_ts(path: Path) -> int:
    """Timestamp on an event file's first line, or 0."""
    try:
        return int(path.read_text().split("\n", 1)[0])
    except (OSError, ValueError):
        return 0


def read_shell_event(after_ts: int) -> dict | None:
    """A finished-command event newer than after_ts, from the fish hook."""
    try:
        lines = SHELL_EVENT_FILE.read_text().splitlines()
        ts, status, duration = int(lines[0]), int(lines[1]), int(lines[2])
    except (OSError, ValueError, IndexError):
        return None
    if ts <= after_ts:
        return None
    return {
        "ts": ts,
        "status": status,
        "duration": duration,
        "cmd": "\n".join(lines[3:]).strip(),
    }


def read_vim_state() -> dict | None:
    try:
        stat = VIM_STATE_FILE.stat()
        if time.time() - stat.st_mtime > VIM_STATE_MAX_AGE:
            return None
        return json.loads(VIM_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def poll_inbox() -> list[str]:
    """Consume messages dropped by `claude-pair say`."""
    try:
        files = sorted(INBOX_DIR.glob("msg-*.txt"))
    except OSError:
        return []
    messages = []
    for path in files:
        try:
            text = path.read_text().strip()
            path.unlink()
        except OSError:
            continue
        if text:
            messages.append(text)
    return messages


def start_stdin_reader(inbox: "queue.Queue[str]") -> None:
    """Lines typed into the watcher pane become direct messages."""

    def reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    inbox.put(line)
        except (OSError, ValueError):
            pass

    threading.Thread(target=reader, daemon=True).start()


def build_snapshot(
    pane_text: str,
    vim_state: dict | None,
    user_messages: list[str] | None = None,
    pane: str = "",
    returned_minutes: float | None = None,
    journal: str = "",
    shell_event: dict | None = None,
) -> str:
    parts = []
    if returned_minutes is not None:
        parts.append(f"<returned after_minutes={int(returned_minutes)}>")
    if journal:
        parts.append(f"<journal_recent>\n{journal}\n</journal_recent>")
    for msg in user_messages or []:
        parts.append(f"<user_message>\n{msg}\n</user_message>")
    if shell_event:
        parts.append(
            '<last_command status="{st}" duration="{dur}s">\n{cmd}\n'
            "</last_command>".format(
                st=shell_event["status"],
                dur=shell_event["duration"],
                cmd=scrub_secrets(shell_event["cmd"]),
            )
        )
    parts.append(
        f'<terminal pane="{pane}">\n{scrub_secrets(pane_text)}\n</terminal>'
    )
    if vim_state and vim_state.get("context"):
        first = vim_state.get("first_line", 1)
        lines = scrub_secrets("\n".join(vim_state["context"])).splitlines()
        numbered = "\n".join(
            f"{first + i:>5} {line}" for i, line in enumerate(lines)
        )
        parts.append(
            "<vim file={file} filetype={ft} cursor_line={line} mode={mode} "
            "unsaved_changes={mod}>\n{body}\n</vim>".format(
                file=json.dumps(vim_state.get("file", "")),
                ft=vim_state.get("filetype", ""),
                line=vim_state.get("line", 0),
                mode=vim_state.get("mode", ""),
                mod=bool(vim_state.get("modified")),
                body=numbered,
            )
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# output formatting


class Printer:
    def __init__(self) -> None:
        self.console = Console(highlight=False)

    def banner(self, text: str) -> None:
        self.console.print(text, style="dim")

    def divider(self) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.console.print()
        self.console.print(
            Rule(title=f"[bold cyan]✻[/] [dim]{stamp}[/]", style="cyan", align="left")
        )

    def stream(self, text: str) -> None:
        # plain passthrough (dry-run output)
        sys.stdout.write(text)
        sys.stdout.flush()

    def live_suggestion(self, refresh_per_second: int = 8) -> Live:
        """A live-updating region the suggestion streams into as markdown."""
        return Live(
            console=self.console,
            refresh_per_second=refresh_per_second,
            vertical_overflow="visible",
        )

    def note(self, text: str) -> None:
        self.console.print(text, style="yellow")

    def tick(self) -> None:
        # quiet heartbeat for SKIP responses
        self.console.print("·", style="dim", end="")

    def timing(self, ttft: float, total: float) -> None:
        self.console.print(
            f" [⧗ {ttft:.1f}s→first · {total:.1f}s total]", style="dim", end=""
        )


# ---------------------------------------------------------------------------
# Claude


class Suggester:
    def __init__(self, args: argparse.Namespace, printer: Printer) -> None:
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.args = args
        self.printer = printer
        self.own_pane = os.environ.get("TMUX_PANE")
        self.messages: list[dict] = []

    def _trim_history(self) -> None:
        # keep the last N user/assistant pairs; history must start with "user"
        max_msgs = self.args.history * 2
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]
            while self.messages and self.messages[0]["role"] != "user":
                self.messages.pop(0)

    def suggest(
        self, snapshot: str, context_text: str = "", vim_context: str = ""
    ) -> None:
        self.context_text = context_text
        self.vim_context = vim_context
        self.messages.append({"role": "user", "content": snapshot})
        self._trim_history()
        try:
            self._call()
        except self.anthropic.RateLimitError as exc:
            retry_after = int(exc.response.headers.get("retry-after", "30"))
            self.printer.note(f"\n[rate limited; pausing {retry_after}s]")
            self.messages.pop()  # snapshot not answered; drop it
            time.sleep(retry_after)
        except self.anthropic.APIStatusError as exc:
            self.printer.note(f"\n[api error {exc.status_code}: {exc.message}]")
            self.messages.pop()
            time.sleep(5)
        except self.anthropic.APIConnectionError:
            self.printer.note("\n[connection error; will retry on next change]")
            self.messages.pop()
            time.sleep(5)

    def _system(self) -> list[dict]:
        # System = frozen prompt + (optional) manually loaded context, then
        # (optional) auto vim-files context. The vim block changes on every
        # save, so it gets its own trailing cache breakpoint — a save
        # re-caches only that block, not the prompt/manual prefix.
        system = [{"type": "text", "text": SYSTEM_PROMPT}]
        if getattr(self, "context_text", ""):
            system.append({"type": "text", "text": self.context_text})
        system[-1]["cache_control"] = {"type": "ephemeral"}
        if getattr(self, "vim_context", ""):
            system.append({
                "type": "text",
                "text": self.vim_context,
                "cache_control": {"type": "ephemeral"},
            })
        return system

    def journal_stretch(self, reason: str) -> None:
        """One small call summarizing the recent work stretch into the journal."""
        if not self.messages:
            return
        tail = journal_tail(12)
        prior = (
            f"Recent entries — continue the story, don't re-cover them:\n{tail}\n"
            if tail else ""
        )
        request = (
            "<journal_request>\n"
            f"({reason}) Write the next entry for the user's work journal.\n"
            f"{prior}</journal_request>"
        )
        try:
            response = self.client.messages.create(
                model=self.args.model,
                max_tokens=400,
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                cache_control={"type": "ephemeral"},
                system=self._system(),
                messages=self.messages + [{"role": "user", "content": request}],
            )
        except (self.anthropic.APIError, self.anthropic.APIConnectionError):
            return  # journaling is best-effort
        log_usage("journal", self.args.model, response.usage)
        entry = " ".join(
            "".join(b.text for b in response.content if b.type == "text").split()
        ).strip("- ")
        if entry and entry.upper() != "SKIP":
            journal_append(entry)
            self.printer.banner("✎ journal updated")

    def startup_brief(self, journal_text: str, age_minutes: float) -> str | None:
        """Summarize the journal tail into a short 'where you left off'.

        Also pre-warms the prompt cache (system prompt + contexts) for the
        first real suggestion. Returns None on any failure — callers fall
        back to printing the raw journal tail.
        """
        request = (
            "<journal_request>\nThe user just reopened claude-pair after "
            f"being away ({_ago(age_minutes)}). From their journal below, "
            "write a 'where you left off' brief: 2-3 short lines — what "
            "they were working on, where they left off, and the obvious "
            "next step. Simple markdown bullets, no headers, no preamble.\n\n"
            f"{journal_text}\n</journal_request>"
        )
        try:
            response = self.client.messages.create(
                model=self.args.model,
                max_tokens=400,
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                system=self._system(),
                messages=[{"role": "user", "content": request}],
            )
        except (self.anthropic.APIError, self.anthropic.APIConnectionError):
            return None
        log_usage("brief", self.args.model, response.usage)
        text = "".join(
            b.text for b in response.content if b.type == "text"
        ).strip()
        return text or None

    def _call(self) -> None:
        system = self._system()

        kwargs = dict(
            model=self.args.model,
            max_tokens=4000,
            thinking={"type": "adaptive"} if self.args.think else {"type": "disabled"},
            output_config={"effort": self.args.effort},
            cache_control={"type": "ephemeral"},
            system=system,
            messages=self.messages,
        )

        self._stream(kwargs)

    def _stream(self, base_kwargs: dict) -> None:
        kwargs = dict(base_kwargs)
        stream_fn = self.client.messages.stream

        buffered = ""
        live: Live | None = None
        t0 = time.monotonic()
        t_first: float | None = None
        try:
            with stream_fn(**kwargs) as stream:
                for text in stream.text_stream:
                    if t_first is None:
                        t_first = time.monotonic()
                    buffered += text
                    if live is None:
                        stripped = buffered.lstrip()
                        if stripped and not "SKIP".startswith(stripped[:4].upper()):
                            # definitely not a SKIP — start rendering it
                            self.printer.divider()
                            live = self.printer.live_suggestion()
                            live.start()
                    if live is not None:
                        live.update(
                            Markdown(buffered.strip(), code_theme=self.args.theme)
                        )
                final = stream.get_final_message()
        finally:
            if live is not None:
                live.stop()

        reply = "".join(
            block.text for block in final.content if block.type == "text"
        ).strip()

        direct = bool(
            self.messages and "<user_message>" in str(self.messages[-1]["content"])
        )
        log_usage(
            "suggest",
            self.args.model,
            final.usage,
            {"class": classify_reply(reply, direct)},
        )

        if final.stop_reason == "refusal":
            self.printer.note("[claude declined to comment on this snapshot]")
        elif live is None:
            self.printer.tick()
        else:
            self._save_suggestion(reply)
            if self.args.hints:
                if extract_diff(reply).strip():
                    self.printer.banner("⌨ ca applies this diff · cs full · ch keys")
                elif extract_code(reply).strip():
                    self.printer.banner("⌨ cl pastes this code · cs full · ch keys")
                else:
                    self.printer.banner("⌨ cs full · good/bad to rate · ch keys")
            if self.args.notify:
                notify_status(self.own_pane, summarize(reply))

        if self.args.timing and t_first is not None:
            self.printer.timing(t_first - t0, time.monotonic() - t0)

        # keep the assistant turn (including SKIP) so the model knows what it
        # already said and doesn't repeat itself
        self.messages.append({"role": "assistant", "content": reply or "SKIP"})

    @staticmethod
    def _save_suggestion(reply: str) -> None:
        """Persist the suggestion for `claude-pair last` and :ClaudeLast."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{stamp}]\n{reply}\n"
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            LAST_SUGGESTION_FILE.write_text(entry)
            LAST_CODE_FILE.write_text(extract_code(reply))
            LAST_DIFF_FILE.write_text(extract_diff(reply))
            with SUGGESTION_LOG.open("a") as log:
                log.write(entry + "\n")
        except OSError:
            pass  # persistence is best-effort; never break the watcher


def _split_fences(reply: str) -> list[tuple[str, str]]:
    """All fenced blocks as (language, contents) pairs."""
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    lang = ""
    in_fence = False
    for line in reply.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if in_fence:
                blocks.append((lang, "\n".join(current)))
                current = []
            else:
                lang = stripped[3:].strip().lower()
            in_fence = not in_fence
            continue
        if in_fence:
            current.append(line)
    if in_fence and current:  # unclosed fence (response cut short)
        blocks.append((lang, "\n".join(current)))
    return [(l, t) for l, t in blocks if t.strip()]


def extract_code(reply: str) -> str:
    """Fenced code blocks (diffs excluded — those go to last_diff.txt)."""
    blocks = [t for l, t in _split_fences(reply) if l != "diff"]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def extract_diff(reply: str) -> str:
    """The contents of ```diff fences, for :ClaudeApply."""
    blocks = [t for l, t in _split_fences(reply) if l == "diff"]
    return "\n\n".join(blocks) + "\n" if blocks else ""


# ---------------------------------------------------------------------------
# main loop


def watch(args: argparse.Namespace) -> None:
    printer = Printer()
    if args.model == "claude-opus-5" and not args.think and args.effort in ("xhigh", "max"):
        # Opus 5 rejects thinking-disabled at xhigh/max effort (400)
        printer.note(f"effort {args.effort} on {args.model} requires thinking; enabling it")
        args.think = True
    mode = "pinned to" if args.pin else "following active pane, starting at"
    think = "thinking" if args.think else "no-think"
    printer.banner(
        f"claude-pair {mode} {args.target} "
        f"(model={args.model}, effort={args.effort}, {think}, "
        f"debounce={args.debounce}s)"
    )
    printer.banner(
        "talk: type here + Enter · claude-pair say · `# claude: ...` at your "
        "prompt · visual <leader>cq in vim"
    )
    printer.banner(
        "keys: cl paste · ca apply diff · cs show · "
        "`claude-pair keys` for the full cheatsheet"
    )

    if not args.dry_run and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        printer.note(
            "note: ANTHROPIC_API_KEY is not set; relying on an "
            "`ant auth login` profile if one exists"
        )

    suggester = None if args.dry_run else Suggester(args, printer)

    stdin_inbox: "queue.Queue[str]" = queue.Queue()
    start_stdin_reader(stdin_inbox)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    poll_inbox()  # discard messages queued before we started

    # --context replaces the store; without it, whatever was loaded persists
    if args.context is not None:
        for f in CONTEXT_DIR.glob("*.ctx"):
            f.unlink()
        for line in add_context_paths(args.context, args.context_budget):
            printer.banner(f"context: {line}")
    context_sig = context_signature()
    context_text = load_context_text()
    if context_text:
        printer.banner(f"context: {len(context_text)} chars loaded")

    vim_sig = vim_context_signature(args.vim_files)
    vim_context = build_vim_context(args.vim_files, args.context_budget)
    if vim_context:
        nfiles = len(read_recent_vim_files(args.vim_files))
        printer.banner(f"vim context: {nfiles} recent file(s), "
                       f"{len(vim_context)} chars")

    own_pane = os.environ.get("TMUX_PANE")
    if own_pane:  # let `claude-pair hide/show/toggle` find this watcher
        try:
            PANE_FILE.write_text(own_pane)
        except OSError:
            pass
    target = args.target

    last_hash = None
    last_change_at = None
    analyzed_hash = None
    last_call_at = 0.0

    # away detection uses wall-clock time: monotonic clocks don't advance
    # during laptop suspend, which is exactly the "stepped away" case.
    # tmux's #{client_activity} (last real keypress) is the primary signal —
    # it ignores background output landing in the pane while the user is
    # gone; pane changes are the fallback when no client is attached.
    away_secs = args.away * 60
    last_activity_wall = time.time()
    returned_minutes: float | None = None
    have_client = False
    first_analysis = True

    # fresh start after a long gap (Monday morning, back from lunch):
    # summarize where the journal left off, right away, before any activity
    journal_age = journal_age_minutes()
    if away_secs > 0 and journal_age is not None and journal_age * 60 >= away_secs:
        section = journal_last_section()
        if section:
            printer.console.print()
            printer.console.print(Rule(
                title=f"[bold cyan]✻[/] [dim]where you left off "
                      f"({_ago(journal_age)})[/]",
                style="cyan", align="left",
            ))
            brief = None
            if suggester is not None:
                suggester.context_text = context_text
                suggester.vim_context = vim_context
                with printer.console.status("[dim]summarizing…[/]"):
                    brief = suggester.startup_brief(journal_tail(40), journal_age)
            if brief:
                printer.console.print(Markdown(brief, code_theme=args.theme))
                # already re-grounded; skip the first-snapshot return recap
            else:
                # offline / no key / dry-run: raw tail, and let the first
                # snapshot carry the <returned> marker for a model recap
                printer.console.print(Markdown(section))
                returned_minutes = journal_age

    # precise completion signals from the fish/vim hooks (ignore stale ones)
    last_shell_ts = event_ts(SHELL_EVENT_FILE)
    last_editor_ts = event_ts(EDITOR_EVENT_FILE)
    shell_event: dict | None = None
    force_analysis = False

    # journal-stretch bookkeeping: summarize on break, checkpoint, and exit
    last_journal_wall = time.time()
    suggest_calls = 0

    def journal_checkpoint(reason: str) -> None:
        nonlocal last_journal_wall, suggest_calls
        if suggester and args.journal_every > 0 and suggest_calls > 0:
            suggester.journal_stretch(reason)
        last_journal_wall = time.time()
        suggest_calls = 0

    args._journal_exit = journal_checkpoint  # main() runs this on shutdown

    def note_activity(now_wall: float) -> None:
        nonlocal last_activity_wall, returned_minutes
        gap = now_wall - last_activity_wall
        if away_secs > 0 and gap >= away_secs:
            returned_minutes = gap / 60
            printer.banner(f"→ welcome back ({int(returned_minutes)} min away)")
        last_activity_wall = max(last_activity_wall, now_wall)

    while True:
        act = client_activity_wall()
        have_client = act is not None
        if have_client and act > last_activity_wall:
            note_activity(act)

        if not args.pin:
            active = resolve_active_pane(own_pane)
            if active and active != target:
                if not have_client:  # infer activity from the switch itself
                    note_activity(time.time())
                target = active
                printer.banner(f"→ following {target}")
                # new pane: start change-detection fresh
                last_hash = None
                last_change_at = None
                analyzed_hash = None

        try:
            pane_text = capture_pane(target, args.scrollback)
        except RuntimeError as exc:
            if not args.pin:
                # the watched pane went away; fall back until a new one is active
                printer.banner(f"→ {target} closed; waiting for an active pane")
                last_hash = None
                time.sleep(args.interval)
                continue
            printer.note(f"\nclaude-pair: {exc} (pane closed?) — exiting")
            return

        digest = hashlib.sha256(pane_text.encode()).hexdigest()
        now = time.monotonic()
        if digest != last_hash:
            if not have_client and last_hash is not None:
                note_activity(time.time())  # fallback: change implies activity
            last_hash = digest
            last_change_at = now

        # pick up context added/cleared live via `claude-pair context ...`
        new_sig = context_signature()
        if new_sig != context_sig:
            context_sig = new_sig
            context_text = load_context_text()
            printer.banner(
                f"→ context updated ({len(context_text)} chars)"
                if context_text else "→ context cleared"
            )

        # refresh auto vim-files context when a tracked file is saved or the
        # recent-files ring changes; banner only when the *set* changes
        new_vim_sig = vim_context_signature(args.vim_files)
        if new_vim_sig != vim_sig:
            old_paths = {entry[0] for entry in vim_sig}
            vim_sig = new_vim_sig
            vim_context = build_vim_context(args.vim_files, args.context_budget)
            if {entry[0] for entry in new_vim_sig} != old_paths:
                nfiles = len(read_recent_vim_files(args.vim_files))
                printer.banner(f"→ vim context: {nfiles} recent file(s)")

        # hook events: a finished command rides along in the next snapshot
        # (a failure analyzes immediately); a vim save analyzes promptly too
        new_shell = read_shell_event(last_shell_ts)
        if new_shell:
            last_shell_ts = new_shell["ts"]
            shell_event = new_shell
            if new_shell["status"] != 0:
                force_analysis = True
        editor_ts = event_ts(EDITOR_EVENT_FILE)
        if editor_ts > last_editor_ts:
            last_editor_ts = editor_ts
            force_analysis = True

        # direct messages jump the queue: no debounce, no cooldown
        direct = poll_inbox()
        while not stdin_inbox.empty():
            direct.append(stdin_inbox.get_nowait())

        # journal the pre-break stretch right when a return is detected (the
        # history is still pre-break), or checkpoint a long working stretch
        if returned_minutes is not None:
            journal_checkpoint(
                "the user stepped away and just returned; summarize the "
                "stretch before the break, ending with where they left off"
            )
        elif time.time() - last_journal_wall >= args.journal_every * 60 > 0:
            journal_checkpoint("periodic checkpoint of the current work stretch")

        settled = last_change_at is not None and now - last_change_at >= args.debounce
        cooled = now - last_call_at >= args.cooldown
        pane_is_new = digest != analyzed_hash

        # hook events skip the debounce (the "did it settle?" guess) but
        # respect the cooldown; direct messages skip both
        if direct or (cooled and (force_analysis or (pane_is_new and settled))):
            analyzed_hash = digest
            last_call_at = now
            # journal tail rides along only when memory is missing: the first
            # call of this process, or a welcome-back after a break
            include_journal = first_analysis or returned_minutes is not None
            snapshot = build_snapshot(
                pane_text, read_vim_state(), direct, pane=target,
                returned_minutes=returned_minutes,
                journal=journal_tail() if include_journal else "",
                shell_event=shell_event,
            )
            shell_event = None
            force_analysis = False
            returned_minutes = None
            first_analysis = False
            if args.dry_run:
                printer.divider()
                if context_text:
                    printer.stream(f"[+{len(context_text)} chars context]\n")
                if vim_context:
                    printer.stream(f"[+{len(vim_context)} chars vim context]\n")
                printer.stream(snapshot + "\n")
            else:
                suggester.suggest(snapshot, context_text, vim_context)
                suggest_calls += 1

        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# launcher: split a side pane and re-run ourselves inside it


def launch_split(args: argparse.Namespace, extra_argv: list[str]) -> None:
    if not os.environ.get("TMUX"):
        sys.exit("claude-pair: run this inside a tmux session")
    target = subprocess.run(
        ["tmux", "display-message", "-p", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    inner = [sys.executable, "-m", "claude_pair", "--target", target, *extra_argv]
    # keep the pane open briefly on crash so the error is readable
    cmd = " ".join(shlex.quote(part) for part in inner) + " || sleep 15"
    subprocess.run(
        ["tmux", "split-window", "-dh", "-l", str(args.width), cmd],
        check=True,
    )
    print(f"claude-pair: watching pane {target} in a new side pane")


def say(words: list[str]) -> None:
    """`claude-pair say <message>` — send a direct message to the watcher."""
    message = " ".join(words).strip()
    if not message:
        sys.exit("usage: claude-pair say <message>")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (INBOX_DIR / f"msg-{time.time_ns()}.txt").write_text(message)


def context_cmd(argv: list[str]) -> None:
    """`claude-pair context [add <path>... | clear | list]`."""
    sub = argv[0] if argv else "list"
    if sub == "add":
        if len(argv) < 2:
            sys.exit("usage: claude-pair context add <path>...")
        for line in add_context_paths(argv[1:], DEFAULT_CONTEXT_BUDGET):
            print(line)
    elif sub == "clear":
        removed = 0
        for f in CONTEXT_DIR.glob("*.ctx"):
            f.unlink()
            removed += 1
        print(f"context cleared ({removed} source(s))")
    elif sub == "list":
        items = sorted(CONTEXT_DIR.glob("*.ctx")) if CONTEXT_DIR.is_dir() else []
        if not items:
            print("no context loaded")
            return
        for f in items:
            head = f.read_text().splitlines()[0].removeprefix("# source: ")
            print(f"{head}  ({f.stat().st_size} chars)")
    else:
        sys.exit("usage: claude-pair context [add <path>... | clear | list]")


def keys_cmd() -> None:
    """`claude-pair keys` — the full keybinding / command cheatsheet."""
    console = Console(highlight=False)
    from rich.table import Table

    vim = Table(title="vim (defaults — :ClaudeKeys shows your actual maps)",
                title_style="dim", title_justify="left",
                show_header=False, show_edge=False, pad_edge=False)
    vim.add_column(style="bold cyan")
    vim.add_column()
    for key, desc in (
        ("<leader>cl", "paste last suggestion's code at cursor"),
        ("<leader>ca", "apply last diff to buffer (:ClaudeApply! forces)"),
        ("<leader>cq", "ask about selection (visual mode)"),
        ("<leader>cc", "send whole file as reference context"),
        ("<leader>cs", "show full last suggestion"),
        ("<leader>ch", "keybinding cheatsheet in vim"),
    ):
        vim.add_row(key, desc)

    shell = Table(title="shell", title_style="dim", title_justify="left",
                  show_header=False, show_edge=False, pad_edge=False)
    shell.add_column(style="bold cyan")
    shell.add_column()
    for cmd, desc in (
        ('claude-pair say "..."', "ask directly (# claude: ... at the prompt too)"),
        ("claude-pair last [--code]", "recall last suggestion / just its code"),
        ("claude-pair good/bad [..]", "rate the last suggestion"),
        ("claude-pair hide|show|toggle", "stash/restore the pane (keeps running)"),
        ("claude-pair context add|list|clear", "manage loaded reference context"),
        ("claude-pair journal | standup | costs", "activity log · standup · spend"),
        ("claude-pair update", "pull latest + reinstall + PlugUpdate"),
    ):
        shell.add_row(cmd, desc)

    console.print(vim)
    console.print()
    console.print(shell)
    console.print()
    console.print(
        'tmux: bind h run-shell "claude-pair toggle"  (one-key hide/show)',
        style="dim",
    )


def feedback_cmd(rating: str, words: list[str]) -> None:
    """`claude-pair good|bad [comment]` — steer the session, log the vote."""
    comment = " ".join(words).strip()
    message = f"FEEDBACK {rating}" + (f": {comment}" if comment else "")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (INBOX_DIR / f"msg-{time.time_ns()}.txt").write_text(message)

    about = ""
    try:  # first content line of the suggestion being rated
        lines = LAST_SUGGESTION_FILE.read_text().strip().splitlines()
        about = next((l for l in lines[1:] if l.strip()), "")[:160]
    except OSError:
        pass
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with FEEDBACK_FILE.open("a") as f:
            f.write(json.dumps({
                "ts": int(time.time()),
                "rating": rating,
                "comment": comment,
                "about": about,
            }) + "\n")
    except OSError:
        pass
    print(f"claude-pair: noted ({rating}{': ' + comment if comment else ''})")


def last(argv: list[str]) -> None:
    """`claude-pair last` — print the most recent suggestion (rendered).

    `claude-pair last --code` prints just the fenced code, raw — suitable
    for piping (e.g. `claude-pair last --code | fish_clipboard_copy`).
    """
    if "--code" in argv:
        try:
            code = LAST_CODE_FILE.read_text()
        except OSError:
            code = ""
        if not code.strip():
            sys.exit("claude-pair: no code in the last suggestion")
        sys.stdout.write(code)
        return
    try:
        text = LAST_SUGGESTION_FILE.read_text()
    except OSError:
        sys.exit("claude-pair: no suggestion yet")
    console = Console(highlight=False)
    lines = text.splitlines()
    if lines and lines[0].startswith("["):
        console.print(lines[0], style="dim")
        text = "\n".join(lines[1:])
    console.print(Markdown(text.strip(), code_theme="monokai"))


def update() -> None:
    """`claude-pair update` — pull the repo, reinstall, refresh vim-plug."""
    repo = Path(__file__).resolve().parent.parent
    console = Console(highlight=False)
    if not (repo / ".git").is_dir():
        sys.exit(
            f"claude-pair: no git repo at {repo} (not an editable install?) — "
            "update however you installed it"
        )

    console.print(f"→ git pull [dim]({repo})[/]", style="bold")
    pull = subprocess.run(
        ["git", "-C", str(repo), "pull", "--ff-only"],
        capture_output=True,
        text=True,
    )
    output = (pull.stdout + pull.stderr).strip()
    console.print(output, style=None if pull.returncode == 0 else "red")
    if pull.returncode != 0:
        sys.exit(1)

    if "Already up to date" not in output:
        console.print("→ pip install -e .", style="bold")
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-e", str(repo), '--break-system-packages']
        )
        if pip.returncode != 0:
            console.print("pip install failed — fix manually", style="red")

    # vim-plug users have their own clone of this repo; refresh it too.
    # `silent!` makes this a no-op for people who don't use vim-plug.
    if shutil.which("vim") and sys.stdout.isatty():
        console.print("→ vim +PlugUpdate", style="bold")
        subprocess.run(["vim", "+silent! PlugUpdate --sync", "+qa"])
    else:
        console.print(
            "skipping vim +PlugUpdate (no vim or not a terminal)", style="dim"
        )
    console.print("done ✻", style="bold green")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "say":
        say(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "last":
        last(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "context":
        context_cmd(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("hide", "show", "toggle"):
        pane_cmd(sys.argv[1:2])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "journal":
        journal_cmd(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "costs":
        costs_cmd(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("good", "bad"):
        feedback_cmd(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "standup":
        standup_cmd(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "keys":
        keys_cmd()
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("update", "--update"):
        update()
        return

    parser = argparse.ArgumentParser(
        prog="claude-pair",
        description="Claude pair programmer watching your tmux pane. "
        "Run with no --target inside tmux to open a watcher side pane. "
        "`claude-pair say <message>` talks to a running watcher.",
    )
    parser.add_argument(
        "--target", help="tmux pane to start watching (e.g. %%3). Omit to auto-split."
    )
    parser.add_argument(
        "--pin",
        action="store_true",
        help="stay on the launch/--target pane instead of following the "
        "active pane as you move around",
    )
    parser.add_argument(
        "--hints",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="print a one-line keybinding hint under each suggestion "
        "(default on; --no-hints disables)",
    )
    parser.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ping the tmux status line for suggestions when the watcher "
        "pane is on another window (default: on; use --no-notify to disable)",
    )
    parser.add_argument(
        "--away",
        type=float,
        default=60.0,
        metavar="MINUTES",
        help="after this many minutes of inactivity, greet your return with "
        "a recap of what you were doing (default 60; 0 disables)",
    )
    parser.add_argument(
        "--journal-every",
        type=float,
        default=30.0,
        metavar="MINUTES",
        help="checkpoint the work journal after this many minutes of active "
        "work (default 30; 0 disables journaling). Entries are also written "
        "when you step away and when the watcher exits.",
    )
    parser.add_argument(
        "--context",
        action="append",
        metavar="PATH",
        help="file or directory to load as reference context (repeatable). "
        "Replaces any previously-loaded context. Add more later while "
        "running with `claude-pair context add <path>`.",
    )
    parser.add_argument(
        "--context-budget",
        type=int,
        default=DEFAULT_CONTEXT_BUDGET,
        help=f"max chars to load per path (default {DEFAULT_CONTEXT_BUDGET})",
    )
    parser.add_argument(
        "--vim-files",
        type=int,
        default=5,
        metavar="N",
        help="auto-load the full saved contents of the last N files edited "
        "in vim as reference context (default 5; 0 disables)",
    )
    parser.add_argument(
        "--model", default=os.environ.get("CLAUDE_PAIR_MODEL", DEFAULT_MODEL)
    )
    parser.add_argument(
        "--effort",
        default="low",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="reasoning effort per suggestion (default: low, for snappiness)",
    )
    parser.add_argument(
        "--think",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="let the model think before answering (slower, sometimes deeper; "
        "default off for snappier first-token latency)",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="print time-to-first-token and total per call (diagnose "
        "network vs. model latency)",
    )
    parser.add_argument(
        "--theme",
        default="monokai",
        help="pygments theme for code blocks (e.g. monokai, dracula, ansi_dark)",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="pane poll interval, seconds"
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=0.25,
        help="quiet time after a change before asking Claude, seconds",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=2.0,
        help="minimum seconds between Claude calls",
    )
    parser.add_argument(
        "--scrollback",
        type=int,
        default=50,
        help="extra scrollback lines to include beyond the visible pane",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=8,
        help="snapshot/reply pairs of conversation memory to keep",
    )
    parser.add_argument(
        "--width", type=int, default=60, help="width of the auto-split side pane"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print snapshots instead of calling the API (for testing)",
    )
    args = parser.parse_args()

    if args.target:
        try:
            watch(args)
        except KeyboardInterrupt:
            print("\nclaude-pair: bye")
        finally:
            exit_checkpoint = getattr(args, "_journal_exit", None)
            if exit_checkpoint:
                exit_checkpoint("the session is ending; wrap up this work "
                                "stretch, ending with where the user left off")
            PANE_FILE.unlink(missing_ok=True)
    else:
        # forward every flag except --width to the inner invocation
        extra: list[str] = []
        skip_next = False
        for token in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if token == "--width":
                skip_next = True
                continue
            if token.startswith("--width="):
                continue
            extra.append(token)
        launch_split(args, extra)


if __name__ == "__main__":
    main()
