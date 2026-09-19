"""One review of one pull request at one commit: gather, cage, run, read.

Split from the plugin loop so the parts that decide something — the prompt,
the parsing of what the agent said, the id a finding keeps across reviews —
can be tested without a server, a network or a model.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import cage
import github

HERE = Path(__file__).resolve().parent
SHIPPED = HERE / "prompts"
# `idea` is here because the app draws notes at that rank and a prompt of
# somebody's own may ask for it. No shipped prompt does: four ranks is what a
# reviewer can tell apart, and a fifth one fills the list with things nobody
# was going to do.
SEVERITIES = ("critical", "high", "medium", "low", "idea")
STYLES = ("balanced", "security", "clean")



@dataclass
class Finding:
    severity: str
    title: str
    body: str = ""
    path: str | None = None
    line: int | None = None

    @property
    def id(self) -> str:
        """Stable across reviews of the same pull request: the same problem
        found again keeps its id, so a finding the person dismissed stays
        dismissed (the server keeps their status over the plugin's). Keyed
        on where and what, not on the wording of the body, which changes."""
        norm = re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()
        return "f-" + hashlib.sha1(f"{self.path or ''}|{norm}".encode()).hexdigest()[:14]


@dataclass
class Outcome:
    ok: bool
    summary: str = ""
    """Why the chosen prompt was not the one used — an unreadable file, an
    empty one. The review still ran, with the shipped one, and the panel says
    so: a review that quietly stopped following your instructions is worse
    than one that refused."""
    prompt_problem: str | None = None
    intent: str = ""
    findings: list[Finding] = field(default_factory=list)
    cost_usd: float | None = None
    seconds: float = 0.0
    error: str | None = None


def parse_answer(text: str) -> Outcome:
    """The last fenced json block of the agent's answer, shape-checked. A
    finding with an unknown severity or no title is dropped, not guessed at;
    an answer with no block at all is a failed review, not an empty one."""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.S)
    if not blocks:
        return Outcome(ok=False, error="the agent's answer had no json block")
    try:
        d = json.loads(blocks[-1])
    except ValueError as e:
        return Outcome(ok=False, error=f"the agent's json did not parse: {e}")
    out = Outcome(ok=True, summary=str(d.get("summary") or "")[:8000], intent=str(d.get("intent") or "")[:1000])
    for f in d.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev, title = f.get("severity"), f.get("title")
        if sev not in SEVERITIES or not isinstance(title, str) or not title.strip():
            continue
        path = f.get("path") if isinstance(f.get("path"), str) and f.get("path") else None
        if path and (path.startswith("/") or ".." in path.split("/")):
            path = None
        line = f.get("line") if isinstance(f.get("line"), int) and f.get("line") > 0 else None
        out.findings.append(Finding(severity=sev, title=title.strip()[:300], body=str(f.get("body") or "")[:8000],
                                    path=path, line=line if path else None))
    return out


def style_text(style: str, mine: str) -> tuple[str, str | None]:
    """The half of the prompt that says HOW to review.

    Three are shipped, and they are deliberately plain: a review style is a
    matter of taste, and the person who installs this is the one who knows
    what their code needs. Theirs is written in the settings box and kept
    where their other settings are, so an update of this plugin cannot
    overwrite it and a prompt written for one employer's code never travels
    with a public plugin.

    Returns the text and, when something was wrong with it, why. An empty box
    falls back rather than failing the review: an agent reviewing with no
    instructions at all reads as a bad review rather than as a missing prompt.
    """
    if style == "mine":
        if mine.strip():
            return mine.strip(), None
        return shipped("balanced"), "the box for your own prompt is empty"
    return shipped(style if style in STYLES else "balanced"), None


def shipped(name: str) -> str:
    return (SHIPPED / f"{name}.md").read_text(encoding="utf-8")


def build_prompt(files: dict[str, Path], incremental: bool, memory: list[str],
                 style: str = "balanced", mine: str = "", extra: str = "") -> tuple[str, str | None]:
    """The frame the plugin owns, wrapped around the style the person chose.

    The frame carries what the agent cannot guess — where the files are, that
    they are evidence and not orders, and the JSON block this plugin parses —
    so editing the style, or replacing it wholesale, cannot leave a review
    that comes back unreadable.
    """
    body, why = style_text(style, mine)
    if extra.strip():
        body += "\n\nAlso, from the person who asked for this review:\n\n" + extra.strip()
    t = (SHIPPED / "frame.md").read_text(encoding="utf-8")
    delta = (f"- Only what changed since your last review: `{files['delta']}`. Review this part; open the full diff only "
             f"when you need the surroundings.") if incremental else ""
    threads = f"- Earlier review threads, with their state: `{files['threads']}`" if "threads" in files else ""
    if memory:
        rules = ("This pull request was reviewed before. What the person decided about those findings is in "
                 f"`{files['memory']}`. Do not raise again anything they dismissed or resolved unless the code "
                 "still has the problem **and** it is critical or high; if so, say that it was marked handled. "
                 "Never suggest undoing something an earlier review thread asked for.")
    else:
        rules = "Do not repeat anything an OPEN review thread already says."
    return (t.replace("{context}", str(files["context"])).replace("{diff}", str(files["diff"]))
            .replace("{delta_line}", delta).replace("{threads_line}", threads)
            .replace("{memory_rules}", rules).replace("{style}", body), why)


def run(pr: github.Pr, adapter: cage.Adapter, model: str, cap_usd: float, prev_sha: str | None,
        memory: list[str], work_root: Path, on_proc=None, timeout_s: int = 1800,
        style: str = "balanced", mine: str = "", extra: str = "") -> Outcome:
    """Gather everything outside the cage, run the agent inside it, read its
    answer. `on_proc` is handed the running process so the plugin can cancel."""
    started = time.time()
    work = Path(tempfile.mkdtemp(prefix="review-", dir=work_root))
    try:
        ctx_dir = work / "context"
        ctx_dir.mkdir()
        ctx = github.context(pr.repo, pr.number)
        files: dict[str, Path] = {"context": ctx_dir / "pull-request.md", "diff": ctx_dir / "full.diff"}
        files["context"].write_text(
            f"# {ctx['title']}\n\nAuthor: {ctx['author']} · {ctx['head']} → {ctx['base']}\n\n"
            f"## Description\n\n{ctx['body'] or '(none)'}\n\n## Commits\n\n" + "\n".join(f"- {c}" for c in ctx["commits"]),
            encoding="utf-8")
        files["diff"].write_text(github.diff(pr.repo, pr.number), encoding="utf-8")
        incremental = False
        if prev_sha and prev_sha != pr.head:
            delta = github.compare(pr.repo, prev_sha, pr.head)
            if delta is not None:
                files["delta"] = ctx_dir / "since-last-review.diff"
                files["delta"].write_text(delta, encoding="utf-8")
                incremental = True
        if ctx["threads"]:
            files["threads"] = ctx_dir / "review-threads.txt"
            files["threads"].write_text("\n".join(ctx["threads"]), encoding="utf-8")
        if memory:
            files["memory"] = ctx_dir / "earlier-findings.txt"
            files["memory"].write_text("\n".join(memory), encoding="utf-8")

        snap = work / "repo"
        github.snapshot(pr.repo, pr.head, snap)
        # The pull request's own agent configuration (hooks, MCP servers)
        # must not run inside its review. See cage.py.
        cage.sanitize_snapshot(snap)
        prompt, prompt_problem = build_prompt(files, incremental, memory, style, mine, extra)
        c = cage.build(adapter, snap, [ctx_dir], work / "out", work / "agent", prompt, model, cap_usd)
        # `errors="replace"`: an agent printing invalid UTF-8 must not raise
        # out of here with the process still running and Cancel lost.
        proc = subprocess.Popen(c.argv, env=c.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
        if on_proc:
            on_proc(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            # What a stopped run spent is unknown; the cap is what it may
            # have spent, and that is what the daily budget is charged.
            return Outcome(ok=False, error=f"the agent ran past {timeout_s // 60} minutes and was stopped",
                           seconds=time.time() - started, cost_usd=cap_usd)
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode in (-9, -15):
            return Outcome(ok=False, error="cancelled", seconds=time.time() - started, cost_usd=cap_usd)
        text, cost = adapter.parse(stdout, c.out_dir)
        out = parse_answer(text)
        # An agent that does not report cost is charged its cap, so the daily
        # budget still bounds it.
        out.cost_usd = cost if cost is not None else cap_usd
        out.seconds = time.time() - started
        out.prompt_problem = prompt_problem
        if not out.ok and proc.returncode != 0:
            tail = (stderr or stdout or "").strip().splitlines()[-1:] or [""]
            out.error = f"{adapter.label} exited with {proc.returncode}: {tail[0][:240]}"
        return out
    except github.GhError as e:
        return Outcome(ok=False, error=str(e), seconds=time.time() - started)
    except Exception as e:  # anything else still ends the run as failed, never as "running"
        return Outcome(ok=False, error=f"the review stopped on an error: {type(e).__name__}: {e}"[:300],
                       seconds=time.time() - started)
    finally:
        shutil.rmtree(work, ignore_errors=True)
