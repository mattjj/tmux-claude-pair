"""Regenerate the README screenshots (docs/*.svg).

Renders the same rich components the watcher uses, so the screenshots stay
pixel-faithful to the real UI. Run from the repo root:

    python3 docs/make_screenshots.py
"""

from pathlib import Path

from rich.columns import Columns
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

DOCS = Path(__file__).parent
PANE_WIDTH = 58
CODE_THEME = "monokai"


def pane(renderable, title: str) -> Panel:
    return Panel(
        renderable,
        title=f"[dim]{title}[/]",
        title_align="left",
        border_style="bright_black",
        width=PANE_WIDTH + 4,
        padding=(0, 1),
    )


def shell(*lines: str) -> Text:
    out = Text()
    for line in lines:
        out.append_text(Text.from_markup(line))
        out.append("\n")
    return out


def watcher_pane(*chunks) -> Group:
    header = [
        Text("claude-pair watching %0 (model=claude-opus-5, ...)", style="dim"),
        Text("talk to me: type here + Enter, `claude-pair say ...`,", style="dim"),
        Text("or `# claude: ...` in your shell", style="dim"),
    ]
    return Group(*header, *chunks)


def rule(stamp: str) -> Group:
    return Group(
        Text("· · ·", style="dim"),
        Text(),
        Rule(title=f"[bold cyan]✻[/] [dim]{stamp}[/]", style="cyan", align="left"),
    )


def save(name: str, left, right, title: str) -> None:
    console = Console(record=True, width=2 * (PANE_WIDTH + 4) + 2)
    console.print(Columns([left, right], padding=(0, 1)))
    console.save_svg(DOCS / name, title=title)
    print(f"wrote docs/{name}")


# --- screenshot 1: catching a mistake as it's typed ------------------------

left1 = pane(
    shell(
        "[green]~/proj[/] [bold cyan]❯[/] python -m pytest -x",
        "[dim]......................F[/]",
        "[red]FAILED[/] tests/test_stats.py::test_mean",
        "[red]E   assert 2.0 == 3.0[/]",
        "[green]~/proj[/] [bold cyan]❯[/] vim stats.py",
        "",
        "[dim]# … editing in vim …[/]",
        "",
        "  [blue]def[/] [yellow]mean[/](xs):",
        "      [magenta]return[/] sum(xs) / (len(xs) - [cyan]1[/])[bold reverse] [/]",
        "",
    ),
    "fish + vim",
)

suggestion1 = Markdown(
    """\
- `mean` divides by `len(xs) - 1` — that's the
  Bessel-corrected *variance* denominator, not a
  mean. Line 42:

```python
return sum(xs) / len(xs)
```

- that also explains `test_mean`: 6/2 == 3.0""",
    code_theme=CODE_THEME,
)

right1 = pane(watcher_pane(rule("14:02:11"), suggestion1), "claude-pair")
save("screenshot-watch.svg", left1, right1, "claude-pair — spotting a bug mid-edit")


# --- screenshot 2: asking a question with a # claude: comment ---------------

left2 = pane(
    shell(
        "[green]~/proj[/] [bold cyan]❯[/] tar -xf data.tar.zst",
        "[red]tar: Cannot open: No such file or directory[/]",
        "[green]~/proj[/] [bold cyan]❯[/] ls",
        "data.tar.zstd   stats.py   tests/",
        "[green]~/proj[/] [bold cyan]❯[/] # claude: write me a fish",
        "  function that extracts any archive[bold reverse] [/]",
        "",
    ),
    "fish",
)

suggestion2 = Markdown(
    """\
- typo first: the file is `data.tar.zstd`, you
  typed `data.tar.zst`
- here's the function — save with `funcsave x`:

```fish
function x --description 'extract any archive'
    switch $argv[1]
        case '*.tar.*' '*.tgz' '*.tbz2'
            tar -xf $argv[1]
        case '*.zip'
            unzip $argv[1]
        case '*.gz'
            gunzip -k $argv[1]
        case '*'
            echo "x: don't know $argv[1]" >&2
    end
end
```""",
    code_theme=CODE_THEME,
)

right2 = pane(watcher_pane(rule("14:07:48"), suggestion2), "claude-pair")
save("screenshot-ask.svg", left2, right2, "claude-pair — ask with a # claude: comment")
