"""Everything read from GitHub, read here, and only ever read.

The first way is the app: agentglass talks to GitHub with its own access (a
login kept in the system keyring included), and its `/prs/*` read routes
answer for any repository that has a checkout on this machine. The plugin's
box cannot reach a keyring, so that is the only way a keyring-only login
works. The second is `gh` in this process, which needs its token in a file.
What the app does not offer (the list of reachable repositories) is read
with `gh` alone.

No worktree is ever created. The pull request's code is the exact head commit
extracted into a scratch directory: from the app's `/prs/tree-at` when it has
a checkout of the repository (read from git objects, nothing is checked out),
else from GitHub's tarball through `gh`. So a review never creates a
worktree, a branch, a ref or an object in any repository on this machine,
which matters most for the repositories that belong to somebody else.

Every command here is a read: `gh pr list`, `gh pr view`, `gh pr diff`,
`gh api` with GET. There is no path in this file that writes to GitHub, and
the test suite checks the source for one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import tools

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PR_REF_RE = re.compile(r"^\s*([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})\s*#\s*(\d{1,9})\s*$")
URL_RE = re.compile(r"^\s*https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})/pull/(\d{1,9})\b")


class GhError(RuntimeError):
    pass


def parse_pr_ref(text: str) -> tuple[str, int] | None:
    """`acme/orbit#42` or a pull request URL, or None."""
    m = PR_REF_RE.match(text) or URL_RE.match(text)
    return (m.group(1), int(m.group(2))) if m else None


def _gh_path() -> str:
    """`gh` by where it really is, not by the PATH the app happened to start with."""
    found = tools.find("gh")
    if not found.path:
        raise FileNotFoundError(found.missing("the GitHub CLI (gh)"))
    return found.path


def gh(args: list[str], timeout: int = 60, binary: bool = False) -> str | bytes:
    try:
        r = subprocess.run([_gh_path(), *args], capture_output=True, timeout=timeout, text=not binary)
    except FileNotFoundError as e:
        raise GhError(str(e) if e.args and "looked" in str(e) else "the GitHub CLI (gh) is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GhError(f"gh {args[0]} timed out") from e
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode("utf-8", "replace")
        raise GhError(err.strip().splitlines()[-1][:300] if err.strip() else f"gh {args[0]} failed")
    return r.stdout


def gh_json(args: list[str], timeout: int = 60):
    out = gh(args, timeout)
    try:
        return json.loads(out)
    except ValueError as e:
        raise GhError("gh returned something that is not JSON") from e


BASE = os.environ.get("AGENTGLASS_URL", "http://127.0.0.1:4000").rstrip("/")
TOKEN = os.environ.get("AGENTGLASS_READ_TOKEN", "")
# Why the app could not answer the last time it was asked, in a sentence; the
# self-test shows it, and a `gh` failure that follows is read with it.
app_said: str | None = None
_roots: dict[str, str] = {}


def app_get(path: str, params: dict[str, str], timeout: float = 60) -> dict | None:
    """One GET to the app's own read routes, or None when it cannot answer
    (no token, an older build without the route, a refusal, not running).
    Never raises: the caller goes on to `gh`."""
    global app_said
    if not TOKEN:
        app_said = "the plugin was not given a token"
        return None
    req = urllib.request.Request(f"{BASE}{path}?{urllib.parse.urlencode(params)}", headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = str(json.loads(e.read() or b"{}").get("error") or "")
        except ValueError:
            msg = ""
        app_said = f"{path} answered {e.code}" + (f": {msg}" if msg else "")
        return None
    except (OSError, ValueError) as e:
        app_said = f"{path} did not answer ({str(e)[:80]})"
        return None
    if not isinstance(d, dict) or d.get("ok") is False:
        app_said = f"{path}: {str((d or {}).get('error') or 'not ok')[:200]}"
        return None
    app_said = None
    return d


def app_root(repo: str) -> str | None:
    """The checkout of `repo` on this machine, which is what the app's pull
    request routes are keyed by. None when there is none."""
    if repo not in _roots:
        d = app_get("/prs/locate", {"repo": repo}, 20)
        if not d or not d.get("root"):
            return None
        _roots[repo] = str(d["root"])
    return _roots[repo]


def app_detail(repo: str, number: int) -> dict | None:
    root = app_root(repo)
    d = app_get("/prs/detail", {"root": root, "number": str(number)}) if root else None
    return d.get("detail") if d else None


def _app_head(det: dict) -> str | None:
    """The head commit: the app's own `headSha`, else the last of the pull
    request's commits (an older app), unless that list was cut short, in
    which case it is not known."""
    sha = str(det.get("headSha") or "")
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    commits = det.get("commits") or []
    if commits and not (det.get("truncated") or {}).get("commits"):
        oid = str(commits[-1].get("oid") or "")
        if re.fullmatch(r"[0-9a-f]{40}", oid):
            return oid
    return None


FIX_LOGIN = "gh auth token | gh auth login -h github.com --with-token --insecure-storage"


def unreachable(repos: list[str] | None = None) -> str | None:
    """None when this process can read GitHub, else the sentence that says why
    and what to type. The app is asked first, the way the plugin really reads
    (a pull request of a watched repository, else the login); then `gh` in
    this process, where a box has no keyring and a login stored there works
    in a terminal and fails here."""
    for repo in (repos or [])[:1]:
        if app_root(repo) and app_get("/prs/list", {"root": _roots[repo], "filter": "all", "state": "open"}, 30):
            return None
    if not repos and app_get("/prs/capability", {}, 20):
        return None
    heard = app_said
    try:
        gh(["api", "user"], timeout=20)
    except GhError as e:
        return (f"agentglass could not read GitHub for the plugin ({heard or 'it has no checkout of the watched repository'}) "
                f"and gh cannot either ({e}). gh keeps its login in the system keyring, which this box cannot reach. "
                f"Run once in a terminal: {FIX_LOGIN}")
    return None


_gh_ok: tuple[float, str | None] | None = None


def cannot_read(repo: str) -> str | None:
    """None when `repo` can be read, else the sentence that says why not and
    what to do. Readable means the app has a checkout of it, or `gh` works in
    this process. `gh` is asked once a minute, not once per pull request."""
    global _gh_ok
    if app_root(repo):
        return None
    if _gh_ok is None or time.time() - _gh_ok[0] > 60:
        try:
            gh(["api", "user"], timeout=20)
            _gh_ok = (time.time(), None)
        except GhError as e:
            _gh_ok = (time.time(), str(e))
    if _gh_ok[1] is None:
        return None
    return (f"agentglass has no checkout of {repo} and gh cannot read it from the plugin ({_gh_ok[1]}). Either open or clone "
            f"{repo} in agentglass, or give gh a login kept in a file: {FIX_LOGIN}")


def accessible_repos(limit: int = 2000) -> list[str]:
    """owner/name of every repository this login can reach, most recently
    pushed first."""
    out = gh(["api", "user/repos?per_page=100&affiliation=owner,collaborator,organization_member&sort=pushed",
              "--paginate", "-q", ".[].full_name"], timeout=120)
    names = [n.strip() for n in str(out).splitlines() if REPO_RE.match(n.strip())]
    return names[:limit]


def viewer() -> str:
    d = app_get("/prs/capability", {}, 20)
    if d and d.get("login"):
        return str(d["login"])
    return str(gh_json(["api", "user"])["login"])


@dataclass
class Pr:
    repo: str
    number: int
    title: str
    author: str
    head: str
    base_ref: str
    url: str
    draft: bool

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


PR_FIELDS = "number,title,author,headRefOid,baseRefName,url,isDraft"


def _pr(repo: str, d: dict) -> Pr:
    return Pr(repo=repo, number=int(d["number"]), title=str(d.get("title") or ""),
              author=str((d.get("author") or {}).get("login") or ""), head=str(d["headRefOid"]),
              base_ref=str(d.get("baseRefName") or ""), url=str(d.get("url") or ""), draft=bool(d.get("isDraft")))


def _pr_from_detail(repo: str, det: dict) -> Pr | None:
    head = _app_head(det)
    if not head:
        return None
    return Pr(repo=repo, number=int(det["number"]), title=str(det.get("title") or ""), author=str(det.get("author") or ""),
              head=head, base_ref=str(det.get("baseRefName") or ""), url=str(det.get("url") or ""), draft=bool(det.get("isDraft")))


def labeled(repo: str, label: str) -> list[Pr]:
    root = app_root(repo)
    d = app_get("/prs/list", {"root": root, "filter": "all", "state": "open", "label": label}) if root else None
    if d is not None:
        out = []
        for row in d.get("prs") or []:
            # The app filters on `label` itself; an older build ignores the
            # parameter and sends every open pull request, which would be
            # reviewed, so the label is checked here as well.
            if label not in [str(x.get("name") or "") for x in row.get("labels") or []]:
                continue
            pr = view(repo, int(row["number"]))
            out.append(pr)
        return out[:50]
    rows = gh_json(["pr", "list", "-R", repo, "--state", "open", "--label", label, "--limit", "50", "--json", PR_FIELDS])
    return [_pr(repo, d) for d in rows]


def view(repo: str, number: int) -> Pr:
    det = app_detail(repo, number)
    pr = _pr_from_detail(repo, det) if det else None
    return pr or _pr(repo, gh_json(["pr", "view", str(number), "-R", repo, "--json", PR_FIELDS]))


def _app_context(det: dict) -> dict:
    threads = []
    for t in det.get("threads") or []:
        c = (t.get("comments") or [{}])[0]
        state = "RESOLVED" if t.get("isResolved") else "OUTDATED" if t.get("isOutdated") else "OPEN"
        threads.append(f"[{state}] {t.get('path')}:{t.get('line') or '-'} ({c.get('author') or '?'}): "
                       + str(c.get("body") or "").replace("\n", " ")[:300])
    return {
        "title": det.get("title") or "", "body": det.get("body") or "", "author": det.get("author") or "",
        "base": det.get("baseRefName") or "", "head": det.get("headRefName") or "",
        "commits": [str(c.get("message") or "") for c in det.get("commits") or []][-50:],
        "threads": threads, "threads_ok": not (det.get("truncated") or {}).get("threads"),
    }


def context(repo: str, number: int) -> dict:
    """What a reviewer reads besides the code: what the pull request says it
    does, the commits it is made of, and what reviewers already said."""
    det = app_detail(repo, number)
    if det:
        return _app_context(det)
    d = gh_json(["pr", "view", str(number), "-R", repo, "--json", "title,body,author,commits,labels,baseRefName,headRefName"])
    owner, name = repo.split("/", 1)
    q = """query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){
      reviewThreads(first:100){nodes{isResolved isOutdated path line comments(first:1){nodes{author{login} body}}}}}}}"""
    threads = []
    try:
        # -f for the names: -F would turn an owner called "true" or "123"
        # into a boolean or a number and the threads would silently vanish.
        t = gh_json(["api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={number}"])
        for n in (((t.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads", {}).get("nodes", []):
            c = (n.get("comments") or {}).get("nodes") or [{}]
            state = "RESOLVED" if n.get("isResolved") else "OUTDATED" if n.get("isOutdated") else "OPEN"
            threads.append(f"[{state}] {n.get('path')}:{n.get('line') or '-'} ({(c[0].get('author') or {}).get('login', '?')}): "
                           + str(c[0].get("body") or "").replace("\n", " ")[:300])
        threads_ok = True
    except GhError:
        threads_ok = False
    return {
        "title": d.get("title") or "", "body": d.get("body") or "",
        "author": (d.get("author") or {}).get("login") or "",
        "base": d.get("baseRefName") or "", "head": d.get("headRefName") or "",
        "commits": [str(c.get("messageHeadline") or "") for c in d.get("commits") or []][-50:],
        "threads": threads, "threads_ok": threads_ok,
    }


def diff(repo: str, number: int) -> str:
    root = app_root(repo)
    d = app_get("/prs/diff", {"root": root, "number": str(number)}, 120) if root else None
    if d and isinstance(d.get("text"), str):
        return d["text"]
    return str(gh(["pr", "diff", str(number), "-R", repo], timeout=120))


def compare(repo: str, base: str, head: str, number: int | None = None) -> str | None:
    """What changed between two commits, or None when GitHub cannot say (a
    force-push that dropped `base`). None means review the whole thing.
    The app answers for a pull request it has a checkout of."""
    root = app_root(repo) if number else None
    d = app_get("/prs/compare-diff", {"root": root, "number": str(number), "from": base, "to": head}, 120) if root else None
    if d and isinstance(d.get("text"), str):
        return d["text"]
    try:
        return str(gh(["api", "-H", "Accept: application/vnd.github.diff", f"repos/{repo}/compare/{base}...{head}"], timeout=120))
    except GhError:
        return None


MAX_TARBALL = 400 * 1024 * 1024
MAX_EXTRACTED = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 300_000


def app_tree(repo: str, sha: str, number: int | None, tmp: Path) -> bool:
    """The commit's files from the app's own checkout of `repo`, as a tar written
    to `tmp`. False, with `app_said` saying why, when the app has no checkout,
    does not have the commit, is an older build without the route, or answers
    with a tree of another commit. The caller then goes to `gh`."""
    global app_said
    root = app_root(repo)
    if not root or not TOKEN:
        return False
    params = {"root": root, "sha": sha, **({"number": str(number)} if number else {})}
    req = urllib.request.Request(f"{BASE}/prs/tree-at?{urllib.parse.urlencode(params)}", headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if "x-tar" not in str(r.headers.get("content-type", "")):
                try:
                    app_said = f"/prs/tree-at: {json.loads(r.read() or b'{}').get('error') or 'no tree'}"[:200]
                except ValueError:
                    app_said = "/prs/tree-at did not send a tree"
                return False
            if r.headers.get("x-tree-sha") != sha:
                app_said = "/prs/tree-at sent the tree of another commit"
                return False
            total = 0
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_TARBALL:
                        app_said = f"the repository is over {MAX_TARBALL // (1024 * 1024)} MB at that commit"
                        return False
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        app_said = f"/prs/tree-at answered {e.code}"
        return False
    except OSError as e:
        app_said = f"/prs/tree-at did not answer ({str(e)[:80]})"
        return False
    return True


def snapshot(repo: str, sha: str, dest: Path, number: int | None = None) -> int:
    """The repository at exactly `sha`, into `dest`: from the app's checkout
    when it has one (no GitHub request, so a private repository and a keyring
    login both work), else from GitHub's tarball through `gh`.
    Returns how many entries were skipped as unsafe.

    Streamed to disk with a cap rather than read whole into memory, and
    checked before extracting: the sum of sizes and the number of entries,
    so a zero-filled bomb is refused rather than unpacked. Each entry goes
    through tarfile's `data` filter, which refuses absolute paths, `..`,
    devices and links that leave the directory; one such entry is skipped,
    not fatal — a repository with an absolute symlink is still reviewable."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GhError("not a commit sha")
    tmp = dest.parent / (dest.name + ".tar")
    try:
        if not app_tree(repo, sha, number, tmp):
            _download(repo, sha, tmp)
        return _extract(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _download(repo: str, sha: str, tmp: Path) -> None:
    try:
        with open(tmp, "wb") as f:
            proc = subprocess.Popen([_gh_path(), "api", f"repos/{repo}/tarball/{sha}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            total = 0
            assert proc.stdout is not None
            while chunk := proc.stdout.read(1 << 20):
                total += len(chunk)
                if total > MAX_TARBALL:
                    proc.kill()
                    raise GhError(f"the repository is over {MAX_TARBALL // (1024 * 1024)} MB at that commit")
                f.write(chunk)
            if proc.wait(timeout=600) != 0:
                raise GhError((proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()[-300:] or "gh could not download the tarball")
    except FileNotFoundError as e:
        raise GhError(str(e) if e.args and "looked" in str(e) else "the GitHub CLI (gh) is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GhError("downloading the tarball timed out") from e


def _extract(tmp: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    skipped = 0
    try:
        with tarfile.open(tmp, "r:*") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise GhError(f"the repository has over {MAX_MEMBERS} files at that commit")
            if sum(m.size for m in members if m.isfile()) > MAX_EXTRACTED:
                raise GhError(f"the repository is over {MAX_EXTRACTED // (1024 ** 3)} GB unpacked")
            # GitHub wraps everything in one top directory named after the
            # commit; the app's tar is rooted at "." and has none.
            first = members[0].name if members else ""
            top = "" if first.startswith("./") or first == "." else first.split("/", 1)[0]
            for m in members:
                name = m.name[2:] if m.name.startswith("./") else m.name
                if top:
                    if not name.startswith(top + "/"):
                        continue
                    name = name[len(top) + 1:]
                if not name or name == ".":
                    continue
                m.name = name
                try:
                    tf.extract(m, dest, filter="data")
                except (tarfile.TarError, OSError):
                    skipped += 1
    except tarfile.TarError as e:
        raise GhError(f"the tarball could not be read: {e}") from e
    return skipped
