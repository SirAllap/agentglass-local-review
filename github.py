"""Everything read from GitHub, read here — outside the cage, with the user's
own `gh` login, and only ever read.

No local checkout is involved. The pull request's code comes from GitHub's
tarball of the exact head commit, extracted into a scratch directory; its
diff comes from GitHub's diff of the pull request. So a review never creates
a worktree, a branch, a ref or an object in any repository on this machine,
which matters most for the repositories that belong to somebody else.

Every command here is a read: `gh pr list`, `gh pr view`, `gh pr diff`,
`gh api` with GET. There is no path in this file that writes to GitHub, and
the test suite checks the source for one.
"""
from __future__ import annotations

import json
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PR_REF_RE = re.compile(r"^\s*([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})\s*#\s*(\d{1,9})\s*$")
URL_RE = re.compile(r"^\s*https://github\.com/([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})/pull/(\d{1,9})\b")


class GhError(RuntimeError):
    pass


def parse_pr_ref(text: str) -> tuple[str, int] | None:
    """`acme/orbit#42` or a pull request URL, or None."""
    m = PR_REF_RE.match(text) or URL_RE.match(text)
    return (m.group(1), int(m.group(2))) if m else None


def gh(args: list[str], timeout: int = 60, binary: bool = False) -> str | bytes:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, timeout=timeout, text=not binary)
    except FileNotFoundError as e:
        raise GhError("the GitHub CLI (gh) is not installed") from e
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


def accessible_repos(limit: int = 2000) -> list[str]:
    """owner/name of every repository this login can reach, most recently
    pushed first."""
    out = gh(["api", "user/repos?per_page=100&affiliation=owner,collaborator,organization_member&sort=pushed",
              "--paginate", "-q", ".[].full_name"], timeout=120)
    names = [n.strip() for n in str(out).splitlines() if REPO_RE.match(n.strip())]
    return names[:limit]


def viewer() -> str:
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


def labeled(repo: str, label: str) -> list[Pr]:
    rows = gh_json(["pr", "list", "-R", repo, "--state", "open", "--label", label, "--limit", "50", "--json", PR_FIELDS])
    return [_pr(repo, d) for d in rows]


def view(repo: str, number: int) -> Pr:
    return _pr(repo, gh_json(["pr", "view", str(number), "-R", repo, "--json", PR_FIELDS]))


def context(repo: str, number: int) -> dict:
    """What a reviewer reads besides the code: what the pull request says it
    does, the commits it is made of, and what reviewers already said."""
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
    return str(gh(["pr", "diff", str(number), "-R", repo], timeout=120))


def compare(repo: str, base: str, head: str) -> str | None:
    """What changed between two commits, or None when GitHub cannot say (a
    force-push that dropped `base`). None means review the whole thing."""
    try:
        return str(gh(["api", "-H", "Accept: application/vnd.github.diff", f"repos/{repo}/compare/{base}...{head}"], timeout=120))
    except GhError:
        return None


MAX_TARBALL = 400 * 1024 * 1024
MAX_EXTRACTED = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 300_000


def snapshot(repo: str, sha: str, dest: Path) -> int:
    """The repository at exactly `sha`, from GitHub's tarball, into `dest`.
    Returns how many entries were skipped as unsafe.

    Streamed to disk with a cap rather than read whole into memory, and
    checked before extracting: the sum of sizes and the number of entries,
    so a zero-filled bomb is refused rather than unpacked. Each entry goes
    through tarfile's `data` filter, which refuses absolute paths, `..`,
    devices and links that leave the directory; one such entry is skipped,
    not fatal — a repository with an absolute symlink is still reviewable."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GhError("not a commit sha")
    tmp = dest.parent / (dest.name + ".tar.gz")
    try:
        with open(tmp, "wb") as f:
            proc = subprocess.Popen(["gh", "api", f"repos/{repo}/tarball/{sha}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        raise GhError("the GitHub CLI (gh) is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GhError("downloading the tarball timed out") from e
    dest.mkdir(parents=True, exist_ok=True)
    skipped = 0
    try:
        with tarfile.open(tmp, "r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise GhError(f"the repository has over {MAX_MEMBERS} files at that commit")
            if sum(m.size for m in members if m.isfile()) > MAX_EXTRACTED:
                raise GhError(f"the repository is over {MAX_EXTRACTED // (1024 ** 3)} GB unpacked")
            # GitHub wraps everything in one top directory named after the commit.
            top = members[0].name.split("/", 1)[0] if members else ""
            for m in members:
                if not m.name.startswith(top + "/"):
                    continue
                m.name = m.name[len(top) + 1:]
                if not m.name:
                    continue
                try:
                    tf.extract(m, dest, filter="data")
                except (tarfile.TarError, OSError):
                    skipped += 1
    except tarfile.TarError as e:
        raise GhError(f"the tarball could not be read: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    return skipped
