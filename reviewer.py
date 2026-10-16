"""local-review: the plugin process.

Talks to agentglass only through the plugin channel its token opens
(`/plugin/self/*`): it reads its settings, draws its panel, writes notes on
pull requests, and hears clicks and setting changes as events. Everything it
does on GitHub is a read (github.py). Everything an agent does happens in
the cage (cage.py).

Two threads: this one answers the window (events, redraws) and decides what
needs reviewing; a worker runs one review at a time, because a review is
minutes of an agent and several at once would be several bills at once.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import cage
import github
import review

BASE = os.environ.get("AGENTGLASS_URL", "http://127.0.0.1:4000").rstrip("/")
TOKEN = os.environ.get("AGENTGLASS_READ_TOKEN", "")
# In a box HOME is empty and the plugin's own folder, handed over as
# AGENTGLASS_PLUGIN_DATA, is the only place that survives. A host that does not
# box the plugin does not set it and keeps the folder this plugin always used.
OLD_DATA = Path(os.environ.get("HOME", str(Path.home()))) / ".local" / "share" / "agentglass-local-review"
DATA = Path(os.environ.get("AGENTGLASS_PLUGIN_DATA") or OLD_DATA)
STATE = DATA / "state.json"
PANEL = "reviews"

# A review that failed to reach GitHub is tried again for the same head, a
# little later each time, and then left alone: three tries cost nothing (the
# agent never ran) and a fourth would only repeat the third.
RETRY_MAX = 3

# Findings dismissed with a reason, kept per repository. Oldest go first.
DECISIONS_MAX = 300


def retry_wait(tries: int) -> int:
    """Seconds to wait after the `tries`-th transport failure: 5 min, then 15."""
    return 300 * 3 ** (tries - 1)


def retry_due(done: dict, head: str, now_ms: int) -> bool:
    return (done.get("failed_sha") == head and 0 < done.get("tries", 0) < RETRY_MAX
            and done.get("retry_at", 0) > 0 and now_ms >= done["retry_at"])


def call(method: str, path: str, body: object | None = None, timeout: float = 40) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") | {"ok": False, "status": e.code}
        except ValueError:
            return {"ok": False, "status": e.code}
    except (OSError, ValueError) as e:
        # A restart, a timeout, a body that is not JSON: an answer the loop
        # can back off from, never an exception that ends the plugin.
        return {"ok": False, "status": 0, "error": str(e)[:200]}


def with_github(result: dict, why: str | None) -> dict:
    """The cage result plus the one check the cage cannot make: that GitHub
    answers this process. Without it the panel read "Sandbox holds" while
    every GitHub call failed."""
    checks = {**(result.get("checks") or {}), "GitHub is reachable from the plugin": why is None}
    ok = bool(result.get("ok")) and why is None
    reasons = [x for x in (result.get("why"), why) if x]
    return {**result, "ok": ok, "checks": checks, "why": None if ok else " · ".join(reasons)}


def log(*a: object) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "plugin.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


# --------------------------------------------------------------- state

def migrate_state(old: Path, new: Path) -> None:
    """Carry the review state over from the folder this plugin used before it
    ran in a box, once and only if there is none here yet. The box lists the old
    folder as a read-only grant; anywhere else `old == new` and this is a no-op."""
    if old == new or (new / "state.json").exists():
        return
    try:
        new.mkdir(parents=True, exist_ok=True)
        (new / "state.json").write_bytes((old / "state.json").read_bytes())
    except OSError:
        pass


def load_state() -> dict:
    migrate_state(OLD_DATA, DATA)
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    tmp.replace(STATE)


@dataclass
class Job:
    pr: github.Pr
    full: bool = False
    why: str = "label"
    # The run this job publishes on the pull request, from the moment it is
    # queued: one id for the whole life of a review — queued, running, done.
    # The pull request's own header reads its state from that run, and a fresh
    # id per stage would leave a queued run sitting there forever.
    run_id: str = ""
    title: str = "Review"

    def run(self, **rest) -> dict:
        return {"id": self.run_id, "repo": self.pr.repo, "number": self.pr.number,
                "sha": self.pr.head, "title": self.title, **rest}


class Plugin:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings: dict = {}
        self.state = load_state()
        self.state.setdefault("reviewed", {})
        self.state.setdefault("spend", {})
        self.state.setdefault("history", [])
        self.state.setdefault("manual", [])
        self.prs: dict[str, github.Pr] = {}
        # Queued by hand from the panel: kept apart from what a scan finds, so
        # the next scan does not drop them from the list.
        self.manual: dict[str, github.Pr] = {}
        self.pr_error: str | None = None
        self.queue: "queue.Queue[Job]" = queue.Queue()
        self.queued: set[str] = set()
        # Queued, then cancelled before the worker reached it.
        self.dropped: set[str] = set()
        self.current: Job | None = None
        self.current_started = 0.0
        self.proc: subprocess.Popen | None = None
        self.me: str | None = None
        self.last_scan = 0.0
        self.scanning = threading.Lock()
        self.cage: dict = {"ok": False, "why": "not checked yet", "checks": {}}
        self.cage_for: str | None = None
        self.notice: str | None = None

    # ----------------------------------------------------------- settings

    def check_cage(self) -> None:
        """For the agent chosen now: the cage itself, agentglass on loopback,
        and that the agent starts inside. Again whenever the agent changes."""
        agent = str(self.s("agent", "claude"))
        adapter = cage.ADAPTERS.get(agent)
        result = with_github(cage.self_test(BASE, adapter), github.unreachable([r for r in self.s("repos", []) if github.REPO_RE.match(r)]))
        with self.lock:
            self.cage, self.cage_for = result, agent

    def apply_settings(self, s: dict) -> None:
        with self.lock:
            was = self.settings
            self.settings = s
        """Switched to a prompt of their own, with nothing in the box.

        An empty editor is a worse starting point than a copy: somebody who
        wanted to change one line of Balanced would have to write the other
        fifteen from memory. So the box is filled with whichever shipped
        prompt was in use, once, and only when it is empty — writing it again
        on every settings event would undo their edits on the next keystroke.
        """
        if s.get("style") == "mine" and not str(s.get("myPrompt") or "").strip():
            base = str(was.get("style") or "balanced")
            call("POST", "/plugin/self/settings",
                 {"values": {"myPrompt": review.shipped(base if base in review.STYLES else "balanced")}})
        if self.cage_for != str(self.s("agent", "claude")):
            self.check_cage()
        # The agents actually on this machine, for the Agent select. Verified
        # ones first; the others say so in their label.
        opts = [{"value": a.id, "label": a.label + ("" if a.verified else " (experimental)") + (" — not signed in" if cage.not_signed_in(a) else "")}
                for a in cage.installed_adapters()]
        call("POST", "/plugin/self/options", {"key": "agent", "options": opts})
        threading.Thread(target=self.publish_repos, daemon=True).start()

    def publish_repos(self) -> None:
        """Every repository the person can reach, for the Repositories picker:
        their own, the ones they collaborate on, and their organisations',
        most recently pushed first. Read with their `gh` login, outside any
        cage, and only read."""
        try:
            names = github.accessible_repos()
        except github.GhError as e:
            log("could not list repositories", e)
            return
        call("POST", "/plugin/self/options", {"key": "repos", "options": names})

    def s(self, key: str, default=None):
        v = self.settings.get(key)
        return default if v is None or v == "" else v

    def spent_today(self) -> float:
        return float(self.state["spend"].get(date.today().isoformat(), 0.0))

    # ----------------------------------------------------------- scanning

    def scan(self) -> None:
        """Which pull requests are asking for a review: labelled ones in the
        watched repositories (only the person's own unless they said
        otherwise), plus the ones they added by hand. One at a time: a click
        on Check now during a scan does not start a second one."""
        if not self.scanning.acquire(blocking=False):
            return
        try:
            self._scan()
        finally:
            self.scanning.release()

    def _scan(self) -> None:
        self.last_scan = time.time()
        repos = [r for r in self.s("repos", []) if github.REPO_RE.match(r)]
        label = str(self.s("label", "agentglass-review"))
        found: dict[str, github.Pr] = {}
        err = None
        errors: list[str] = []
        # Each source on its own: one pull request that can no longer be read
        # must not stop every other one from being found.
        if self.me is None and repos:
            try:
                me = github.viewer()
                with self.lock:
                    self.me = me
            except github.GhError as e:
                errors.append(str(e))
        for repo in repos:
            try:
                for pr in github.labeled(repo, label):
                    if self.s("onlyMine", True) and pr.author != self.me:
                        continue
                    found[pr.key] = pr
            except github.GhError as e:
                errors.append(f"{repo}: {e}")
        # Queued by hand before a restart: still listed, not reviewed again
        # unless the person asks.
        for key in list(self.state["manual"]):
            if key in self.manual:
                continue
            parsed = github.parse_pr_ref(key)
            try:
                if parsed:
                    pr = github.view(*parsed)
                    with self.lock:
                        self.manual[key] = pr
            except github.GhError as e:
                errors.append(f"{key}: {e}")
        for ref in self.s("extraPrs", []):
            parsed = github.parse_pr_ref(ref)
            if not parsed:
                continue
            try:
                pr = github.view(*parsed)
                found[pr.key] = pr
            except github.GhError as e:
                errors.append(f"{ref}: {e}")
        err = "; ".join(errors)[:400] if errors else None
        with self.lock:
            self.prs = {**self.manual, **found} if err is None else {**self.prs, **self.manual, **found}
            self.pr_error = err
        for pr in found.values():
            done = self.state["reviewed"].get(pr.key, {})
            if pr.draft and not self.s("reviewDrafts", False):
                continue
            # A head the agent failed on is not retried on its own — at a
            # model's price per attempt that is a bill every few minutes. A new
            # push or a click tries again. One that never reached the agent
            # (GitHub did not answer) is retried, with a wait and a limit.
            if done.get("sha") != pr.head and (done.get("failed_sha") != pr.head or retry_due(done, pr.head, int(time.time() * 1000))):
                self.enqueue(Job(pr=pr, why="new commits" if done else "label"))

    def enqueue(self, job: Job) -> None:
        with self.lock:
            if job.pr.key in self.queued or (self.current and self.current.pr.key == job.pr.key and self.current.pr.head == job.pr.head):
                return
            self.queued.add(job.pr.key)
            agent = str(self.s("agent", "claude"))
            adapter = cage.ADAPTERS.get(agent)
            model = str(self.s("model", ""))
            job.run_id = job.run_id or f"{job.pr.head[:10]}-{int(time.time())}"
            job.title = f"Review · {adapter.label if adapter else agent}" + (f" · {model}" if model else "")
        self.queue.put(job)
        # Published before the review starts, and this is the whole point of a
        # queued run: the button in the pull request's header is the state of
        # this plugin's work on that pull request, and a press that shows
        # nothing until the agent boots is a press that looks ignored.
        call("POST", "/plugin/self/pr/run", job.run(state="queued", startedAt=int(time.time() * 1000)))
        self.draw()

    # ----------------------------------------------------------- the worker

    def worker(self) -> None:
        while True:
            job = self.queue.get()
            with self.lock:
                if job.pr.key in self.dropped:
                    self.dropped.discard(job.pr.key)
                    call("POST", "/plugin/self/pr/run", job.run(
                        state="cancelled", summary="cancelled before it started", finishedAt=int(time.time() * 1000)))
                    self.draw()
                    continue
                self.queued.discard(job.pr.key)
                cap_day = float(self.s("maxUsdPerDay", 10))
                if self.spent_today() >= cap_day:
                    self.notice = f"Today's cap of ${cap_day:.2f} is spent — {job.pr.key} waits for tomorrow or a higher cap."
                    # The queued run has to be taken back, or the pull request
                    # keeps saying "Queued" for a review that is not coming.
                    call("POST", "/plugin/self/pr/run", job.run(
                        state="cancelled", summary=self.notice, finishedAt=int(time.time() * 1000)))
                    self.draw()
                    continue
                self.current, self.current_started = job, time.time()
            try:
                self.review(job)
            except Exception:  # a bug in one review must not stop the next
                log("review crashed", job.pr.key, traceback.format_exc())
            finally:
                with self.lock:
                    self.current, self.proc = None, None
                self.draw()

    def review(self, job: Job) -> None:
        pr = job.pr
        agent_id = str(self.s("agent", "claude"))
        adapter = cage.ADAPTERS.get(agent_id)
        model = str(self.s("model", ""))
        cap = float(self.s("maxUsdPerReview", 2))
        run_id = job.run_id
        run = job.run(state="running", startedAt=int(time.time() * 1000))
        call("POST", "/plugin/self/pr/run", run)
        self.draw()

        def fail(msg: str, cost: float = 0.0, transient: bool = False) -> None:
            call("POST", "/plugin/self/pr/run", run | {"state": "cancelled" if msg == "cancelled" else "failed",
                                                        "summary": msg, "finishedAt": int(time.time() * 1000)})
            self.remember(pr, run_id, None, msg, cost, transient=transient)

        if not self.cage.get("ok"):
            return fail(f"Not reviewed: the sandbox is not safe on this machine — {self.cage.get('why')}")
        if not adapter or not cage.tools.which(adapter.binary):
            return fail(f"Not reviewed: the agent `{agent_id}` is not installed here.")

        unreadable = github.cannot_read(pr.repo)
        if unreadable:
            return fail(f"Not reviewed: {unreadable}", transient=True)
        signed_out = cage.not_signed_in(adapter)
        if signed_out:
            return fail(f"Not reviewed: {signed_out}")

        prev = self.state["reviewed"].get(pr.key, {})
        decisions = self.state.get("decisions", {}).get(pr.repo, {})
        # What was decided for this repository is kept even when the whole pull
        # request is reviewed again: a fresh read is not a reason to forget it.
        memory = review.decision_lines(decisions) + ([] if job.full else [
            f"[{m['status'].upper()}] {m['path'] or '-'} — {m['title']}" for m in prev.get("closed", []) if m["id"] not in decisions])
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "work").mkdir(exist_ok=True)

        def hold(p: subprocess.Popen) -> None:
            with self.lock:
                self.proc = p

        out = review.run(pr, adapter, model, cap, None if job.full else prev.get("sha"), memory, DATA / "work",
                         on_proc=hold, style=str(self.s("style", "balanced")),
                         mine=str(self.s("myPrompt", "")), extra=str(self.s("extraInstructions", "")))
        cost = out.cost_usd or 0.0
        with self.lock:
            day = date.today().isoformat()
            self.state["spend"][day] = round(self.spent_today() + cost, 4)
        if out.prompt_problem:
            # Said out loud, not swallowed: a review that quietly stopped
            # following your own prompt reads as a bad reviewer.
            with self.lock:
                self.notice = f"Reviewed with the shipped prompt: {out.prompt_problem}"
        if not out.ok:
            return fail(out.error or "the review failed", cost, transient=out.transport)

        out.findings, suppressed = review.apply_decisions(out.findings, decisions)
        notes = [{"id": f.id, "runId": run_id, "repo": pr.repo, "number": pr.number, "sha": pr.head,
                  "severity": f.severity, "title": f.title, "body": f.body,
                  **({"path": f.path} if f.path else {}), **({"line": f.line} if f.line else {})} for f in out.findings]
        if notes:
            r = call("POST", "/plugin/self/pr/notes", {"notes": notes})
            if not r.get("ok"):
                log("notes refused", pr.key, r)
        meta = " · ".join(x for x in [f"${cost:.2f}" if out.cost_usd is not None else None, f"{int(out.seconds)} s",
                                        "since last review" if prev.get("sha") and not job.full else "whole pull request",
                                        f"{suppressed} dismissed earlier, not raised" if suppressed else None] if x)
        summary = (f"*{out.intent}*\n\n" if out.intent else "") + out.summary
        call("POST", "/plugin/self/pr/run", run | {"state": "done", "summary": summary, "meta": meta,
                                                    "finishedAt": int(time.time() * 1000)})
        self.remember(pr, run_id, out, None, cost)
        worst = [f for f in out.findings if f.severity in ("critical", "high")]
        notify(f"Review ready: {pr.key}",
               f"{len(out.findings)} findings" + (f", {len(worst)} critical or high" if worst else "") + " — open the pull request in agentglass")

    def remember(self, pr: github.Pr, run_id: str, out: review.Outcome | None, error: str | None, cost: float = 0.0,
                 transient: bool = False) -> None:
        with self.lock:
            entry = self.state["reviewed"].setdefault(pr.key, {})
            if out is not None:
                entry["sha"] = pr.head
                entry.pop("failed_sha", None)
            else:
                tries = entry.get("tries", 0) + 1 if transient and entry.get("failed_sha") == pr.head else 1
                entry["failed_sha"] = pr.head
                entry.pop("retry_at", None)
                if transient:
                    entry["tries"] = tries
                    if tries < RETRY_MAX:
                        entry["retry_at"] = int(time.time() * 1000) + retry_wait(tries) * 1000
                else:
                    entry.pop("tries", None)
            if out is not None:
                entry.pop("tries", None)
                entry.pop("retry_at", None)
            entry["at"] = int(time.time() * 1000)
            entry.setdefault("closed", [])
            # What each finding said, by id, so a status change can be told
            # back to the next review in words rather than as a hash.
            if out is not None:
                seen = entry.setdefault("notes", {})
                for f in out.findings:
                    seen[f.id] = {"title": f.title, "path": f.path}
                if len(seen) > 500:
                    entry["notes"] = dict(list(seen.items())[-500:])
            self.state["history"] = ([{
                "key": pr.key, "title": pr.title, "run": run_id, "sha": pr.head, "at": entry["at"],
                "ok": out is not None, "error": error, "cost": cost,
                "counts": {s: sum(1 for f in out.findings if f.severity == s) for s in review.SEVERITIES} if out else {},
            }] + self.state["history"])[:200]
            save_state(self.state)

    def pr_action(self, ev: dict) -> None:
        """A button this plugin declared was pressed in a pull request's own
        header. The pull request is whichever one the person is reading — it
        need not be labelled, watched, or even theirs, so it is read from
        GitHub if this plugin has never heard of it."""
        key = f"{ev['repo']}#{ev['number']}"
        what = ev.get("id")
        if what == "cancel":
            with self.lock:
                running = self.current and self.current.pr.key == key
                if running and self.proc and self.proc.poll() is None:
                    self.proc.terminate()
                elif key in self.queued:
                    # Out of the queue by name: the worker skips a job whose
                    # key is no longer spoken for, which is cheaper than
                    # rebuilding the queue around one removal.
                    self.queued.discard(key)
                    self.dropped.add(key)
            self.draw()
            return
        pr = self.prs.get(key)
        if pr is None or what in ("review", "review-full"):
            try:
                pr = github.view(ev["repo"], int(ev["number"]))
            except github.GhError as e:
                with self.lock:
                    self.notice = f"Could not read {key}: {e}"
                self.draw()
                return
        with self.lock:
            self.prs[key] = pr
            self.notice = None
        if what == "watch":
            # The same list a pull request queued by hand goes on, so it
            # survives a restart and every push to it is reviewed after that.
            with self.lock:
                self.manual[key] = pr
                if key not in self.state["manual"]:
                    self.state["manual"] = (self.state["manual"] + [key])[-50:]
                    save_state(self.state)
                self.notice = f"{key} will be reviewed on every push."
            self.draw()
            return
        self.enqueue(Job(pr=pr, full=(what == "review-full"), why="asked for"))

    def pr_seen(self, ev: dict) -> None:
        """The person opened this pull request in the app. What the last
        review found has been in front of them, so it stops being news — this
        is what separates Ready from Earlier in the panel, and it is the
        app telling us rather than us guessing from a timestamp."""
        key = f"{ev['repo']}#{ev['number']}"
        with self.lock:
            entry = self.state["reviewed"].get(key)
            if not entry:
                return
            entry["seenAt"] = int(time.time() * 1000)
            save_state(self.state)

    def note_status(self, ev: dict) -> None:
        """The person resolved or dismissed a finding. Kept, so the next
        review of this pull request is told not to raise it again."""
        key = f"{ev['repo']}#{ev['number']}"
        with self.lock:
            entry = self.state["reviewed"].setdefault(key, {})
            closed = [c for c in entry.get("closed", []) if c["id"] != ev["id"]]
            if ev["status"] != "open":
                seen = entry.get("notes", {}).get(ev["id"], {})
                closed.append({"id": ev["id"], "status": ev["status"], "title": seen.get("title", ev["id"]), "path": seen.get("path")})
            entry["closed"] = closed[-200:]
            self.remember_decision(ev, entry)
            save_state(self.state)

    def remember_decision(self, ev: dict, entry: dict) -> None:
        """A dismissal that came with a reason is a decision about the
        repository, not only about this pull request: the same problem in
        another pull request is not raised again. Without a reason there is
        nothing to tell the next review, so it stays about this one."""
        mine = self.state.get("decisions", {}).get(ev["repo"], {})
        if ev["status"] != "dismissed":
            mine.pop(ev["id"], None)
            return
        replies = ev.get("replies") or ([ev["reply"]] if ev.get("reply") else [])
        with_status = [x for x in replies if x.get("withStatus") == "dismissed"]
        said = (with_status or [x for x in replies if x.get("by") == "user"] or [{}])[-1].get("text", "")
        reason = " ".join(str(said).split())[:500]
        if not reason:
            mine.pop(ev["id"], None)
            return
        mine = self.state.setdefault("decisions", {}).setdefault(ev["repo"], {})
        seen = entry.get("notes", {}).get(ev["id"], {})
        mine.pop(ev["id"], None)
        mine[ev["id"]] = {"title": seen.get("title", ev["id"]), "path": seen.get("path"), "status": "dismissed",
                          "reason": reason, "at": ev.get("at")}
        while len(mine) > DECISIONS_MAX:
            mine.pop(next(iter(mine)))

    # ----------------------------------------------------------- drawing

    def draw(self) -> None:
        try:
            tree = self.tree()
            r = call("POST", "/plugin/self/panel", {"id": PANEL, "tree": tree})
            if not r.get("ok"):
                log("panel refused", r)
        except Exception:
            log("draw failed", traceback.format_exc())

    def tree(self) -> dict:
        """The panel is a list of reviews, and nothing else.

        It used to be a dashboard: four counters, a picker of watched pull
        requests, a detail pane for the selected one, and a box to type
        `acme/orbit#42` into. Every one of those answered a question that is
        better answered in the pull request itself — which is where the
        findings are — and together they made the plugin look like the place
        reviews happen, so that is where they were looked for.

        What is left is the one question this view can answer that a pull
        request cannot: WHICH pull requests have been reviewed, and which of
        those you have not read yet. Every row opens its pull request on the
        local lane. Starting a review lives in the pull request's header.
        """
        with self.lock:
            cap_day = float(self.s("maxUsdPerDay", 10))
            spent = self.spent_today()
            now, ready, earlier = self.sections()
            children: list[dict] = [{"type": "row", "align": "between", "children": [
                {"type": "stack", "gap": "sm", "children": [
                    {"type": "heading", "text": "Reviews", "level": 1},
                    {"type": "text", "size": "sm", "tone": "muted",
                     "text": "Read on this machine, written into the pull request, never sent to GitHub."},
                ]},
                {"type": "row", "children": [
                    {"type": "text", "size": "sm", "tone": "danger" if spent >= cap_day else "muted",
                     "text": f"${spent:.2f} of ${cap_day:.2f} today"},
                    {"type": "button", "label": "Check GitHub now", "action": {"id": "scan"}},
                ]},
            ]}]
            if not self.cage.get("ok"):
                children.append({"type": "section", "title": "The sandbox is not safe here",
                                 "subtitle": "No review will run until it is",
                                 "children": [{"type": "text", "tone": "danger", "text": str(self.cage.get("why"))}]})
            for tone, text in [("warning", self.notice), ("danger", self.pr_error)]:
                if text:
                    children.append({"type": "row", "children": [
                        {"type": "badge", "text": "note" if tone == "warning" else "GitHub", "tone": tone},
                        {"type": "text", "size": "sm", "text": text}]})
            if not (now or ready or earlier):
                children.append({"type": "empty", "title": "No reviews yet",
                                 "body": "Open a pull request and press **Local review** in its header. "
                                         f"Or put the `{self.s('label', 'agentglass-review')}` label on one of "
                                         "yours in a watched repository, and every push to it is reviewed."})
            for title, rows, empty in [("Now", now, None), ("Ready — not opened yet", ready, None), ("Earlier", earlier, None)]:
                if not rows:
                    continue
                children.append({"type": "section", "title": title,
                                 "children": [{"type": "list", "items": rows, "empty": empty or "Nothing."}]})
            checks = self.cage.get("checks") or {}
            children.append({"type": "text", "size": "sm", "tone": "muted",
                             "text": f"{'Sandbox holds' if self.cage.get('ok') else 'Check failed: ' + str(self.cage.get('why'))} · {sum(1 for v in checks.values() if v)} of {len(checks)} checks · "
                                     f"watching {len(self.prs)} pull {'request' if len(self.prs) == 1 else 'requests'}"})
            return {"type": "stack", "gap": "lg", "children": children}

    def sections(self) -> tuple[list[dict], list[dict], list[dict]]:
        """The three lists, from what is running and what has run.

        `seenAt` is the app telling us the person opened that pull request
        (see `pr_seen`), so "ready" means a review they have not looked at
        rather than a review from the last hour — a reviewer that keeps
        shouting about findings already read is a reviewer people switch off.
        """
        now: list[dict] = []
        cur = self.current
        if cur:
            elapsed = int(time.time() - self.current_started)
            now.append(self.row(cur.pr.key, cur.pr.title, f"{cur.why} · {cur.title}",
                                [{"text": f"reviewing {elapsed // 60}:{elapsed % 60:02d}", "tone": "accent"}], ""))
        for key in sorted(self.queued):
            pr = self.prs.get(key)
            now.append(self.row(key, pr.title if pr else key, "waiting its turn",
                                [{"text": "queued", "tone": "muted"}], ""))
        ready: list[dict] = []
        earlier: list[dict] = []
        for h in self.state["history"][:40]:
            seen = self.state["reviewed"].get(h["key"], {}).get("seenAt", 0)
            counts = h.get("counts") or {}
            worst = [f"{counts[k]} {k}" for k in ("critical", "high") if counts.get(k)]
            if not h["ok"]:
                badges = [{"text": "failed", "tone": "danger"}]
            elif worst:
                badges = [{"text": ", ".join(worst), "tone": "danger"}]
            elif any(counts.values()):
                badges = [{"text": f"{sum(counts.values())} to look at", "tone": "warning"}]
            else:
                badges = [{"text": "clean", "tone": "success"}]
            if h.get("cost"):
                badges.append({"text": f"${h['cost']:.2f}", "tone": "muted"})
            pr = self.prs.get(h["key"])
            row = self.row(h["key"], h.get("title") or (pr.title if pr else h["key"]),
                           h["error"] if not h["ok"] else f"{h['key']} · reviewed {h['sha'][:7]}",
                           badges, h["run"], at=h["at"])
            (ready if h["ok"] and h["at"] > seen else earlier).append(row)
        return now, ready[:20], earlier[:20]

    def row(self, key: str, title: str, subtitle: str, badges: list[dict], run_id: str, at: int = 0) -> dict:
        ref = github.parse_pr_ref(key)
        item = {"id": f"{key}/{run_id}", "title": title, "subtitle": subtitle, "badges": badges}
        if at:
            item["meta"] = time.strftime("%d %b %H:%M", time.localtime(at / 1000))
        if ref:
            # Every row is a way in: the findings live in the pull request, so
            # a row about them that cannot be clicked into is a dead end.
            item["open"] = {"repo": ref[0], "number": ref[1], "focus": "local"}
        return item

    # ----------------------------------------------------------- events

    def handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "settings":
            self.apply_settings(ev.get("settings") or {})
            threading.Thread(target=self.scan_and_draw, daemon=True).start()
        elif t == "note-status":
            self.note_status(ev)
        elif t == "pr-action":
            self.pr_action(ev)
        elif t == "pr-open":
            self.pr_seen(ev)
        elif t == "action":
            # One button left on the panel. Everything else a person can ask
            # this plugin for, they ask for on the pull request it is about.
            if (ev.get("action") or {}).get("id") == "scan":
                threading.Thread(target=self.scan_and_draw, daemon=True).start()
        self.draw()

    def scan_and_draw(self) -> None:
        try:
            self.scan()
        finally:
            self.draw()

    def run(self) -> None:
        me = call("GET", "/plugin/self")
        if not me.get("ok"):
            log("no self", me)
            raise SystemExit(1)
        self.apply_settings(me.get("settings") or {})
        if self.cage_for is None:
            self.check_cage()
        threading.Thread(target=self.worker, daemon=True).start()
        self.draw()
        threading.Thread(target=self.scan_and_draw, daemon=True).start()
        while True:
            r = call("GET", "/plugin/self/events?wait=25000", timeout=35)
            if r.get("status") in (401, 403):
                # The token was revoked: the plugin was disabled. Leave.
                raise SystemExit(0)
            if not r.get("ok"):
                # The server is restarting or said something odd. Back off
                # rather than spin; if it is gone for good, the server that
                # comes back starts a new copy of this plugin.
                time.sleep(2)
                continue
            for ev in r.get("events") or []:
                try:
                    self.handle(ev)
                except Exception:
                    log("event failed", ev, traceback.format_exc())
            poll = max(60, int(self.s("pollSeconds", 180)))
            if time.time() - self.last_scan > poll:
                threading.Thread(target=self.scan_and_draw, daemon=True).start()
            elif self.current:
                self.draw()  # keep the elapsed time moving


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "agentglass", title, body], capture_output=True, timeout=5)


if __name__ == "__main__":
    Plugin().run()
