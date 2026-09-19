"""The sandbox every reviewing agent runs in, whichever agent it is.

The agent's own permission flags are not what keeps a review local. There are
a dozen agent CLIs, each with its own idea of "read-only", and trusting each
one is trusting a dozen implementations. Instead every agent runs inside one
bubblewrap cage with the same shape:

- the whole disk is mounted read-only;
- the credentials that reach a remote are not there at all: `~/.config/gh`,
  `~/.ssh`, git credential stores, agentglass's own token, the keyring, the
  user's runtime directory (dbus, ssh-agent), and the variables that carry a
  GitHub token;
- the snapshot of the pull request is read-only; one output directory is the
  only thing it may write, and its config directory is a throwaway copy;
- the network stays, because the agent has to reach its model.

So with every permission of its own switched off, an agent in here can read
code and write text into `out/`, and nothing else. Measured with Claude given
`--dangerously-skip-permissions`: it could not create a file, `gh` answered
"not logged in", and `git` over SSH failed.

Hardened after an adversarial review found three ways past the first version:

- The pull request's own agent configuration ran. Claude loads a repository's
  `.claude/settings.json`, hooks included, from its working directory — the
  snapshot — before any model call. Agent config files are now removed from
  the snapshot, and Claude is told to load only user settings, which are the
  throwaway ones.
- The cage shared the host's process table: `pkill -u $USER` inside killed
  processes outside. It now has its own pid, ipc, uts and cgroup namespaces.
- It saw all of `$HOME` and the whole environment: shell history, browser
  profiles, every token in `~/.config`, every API key in env. HOME is now an
  empty tmpfs with only the agent's own install mounted back, and the
  environment is an allowlist.

What this does not stop: the network stays, because the agent has to reach its
model, and without a network namespace per destination it can reach anything
the machine can — including services on loopback. The self-test refuses to
run if agentglass itself answers there without a token. An agent talked into
it by the code it reads could still send that code somewhere, without any
credential. That is why, by default, only your own pull requests are reviewed.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))

# Hidden from the agent: an empty tmpfs over each directory, /dev/null over
# each file. Anything that lets a process act on a remote as this user.
HIDDEN_DIRS = [
    ".config/gh", ".ssh", ".config/agentglass", ".local/share/keyrings", ".gnupg",
    ".config/git/credentials.d", ".docker", ".kube", ".aws", ".config/gcloud",
]
HIDDEN_FILES = [".git-credentials", ".config/git/credentials", ".netrc", ".npmrc", ".pypirc"]
UNSET_ENV = [
    "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "AGENTGLASS_TOKEN", "AGENTGLASS_READ_TOKEN", "DBUS_SESSION_BUS_ADDRESS", "GIT_ASKPASS", "SSH_ASKPASS",
]
# What the agent's environment is made of. Everything else is cleared.
ENV_ALLOW = ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME"]

# Agent configuration a repository can carry, which the agent would load from
# its working directory: removed from the snapshot before the agent starts.
# The diff still shows any change to them; only the live files go.
PROJECT_AGENT_CONFIG = [
    ".claude", ".mcp.json", ".gemini", ".codex", ".opencode", "opencode.json", ".cursor",
    ".crush", "crush.json", ".vscode", ".idea", ".envrc", ".mise.toml", "mise.toml", ".tool-versions",
]


def sanitize_snapshot(snapshot: Path) -> list[str]:
    """Remove the agent configuration a pull request could use to run code
    inside the review (hooks, MCP servers, env). Only at the top level and in
    `.claude`-shaped directories anywhere, which is where CLIs look."""
    removed: list[str] = []
    for name in PROJECT_AGENT_CONFIG:
        for p in [snapshot / name, *snapshot.rglob(name)] if name.startswith(".claude") else [snapshot / name]:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
                removed.append(str(p.relative_to(snapshot)))
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(str(p.relative_to(snapshot)))
    return sorted(set(removed))


@dataclass
class Adapter:
    """How to run one agent CLI once, headless, on a prompt, inside the cage.

    `verified` means the cage was measured with this agent on a real machine
    and its config isolation is known to hold; an unverified adapter still
    runs in the same cage, with its config mounted read-only, which may simply
    make it fail to start. It is never given more to make it work.
    """
    id: str
    label: str
    binary: str
    verified: bool
    # Returns argv. `prompt` is the full prompt text, `model` may be "", and
    # `dirs` are the read-only directories outside the snapshot it may read.
    argv: callable = None  # type: ignore[assignment]
    # Files from the real config that the throwaway config needs, bound
    # read-write at the same relative path so a token refresh lands in the
    # real file and never diverges from it.
    credentials: list[str] = field(default_factory=list)
    # The variable that points the CLI at a config directory, if it has one.
    config_env: str | None = None
    # Directories under HOME the CLI reads its config from; mounted read-only
    # when there is no config_env to redirect it.
    config_dirs: list[str] = field(default_factory=list)
    # Reads the agent's stdout (and out/ if it wrote there) into (text, cost_usd|None).
    parse: callable = None  # type: ignore[assignment]
    # Variables this agent authenticates with, when a person uses a key
    # rather than a login. Passed through; no other agent's are.
    env_keys: list[str] = field(default_factory=list)


def _claude_argv(prompt: str, model: str, cap_usd: float, dirs: list[str]) -> list[str]:
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        # Belt and braces inside the cage: only the three reading tools exist
        # for it, they are allowed without asking, and anything else is
        # refused without a prompt. A fresh config has no allow rules of its
        # own, so without the explicit allow every read was refused — measured.
        "--tools", "Read,Grep,Glob",
        "--allowedTools", "Read,Grep,Glob",
        "--permission-mode", "dontAsk",
        "--strict-mcp-config",
        # Only the throwaway user settings. Without this Claude also loads the
        # snapshot's `.claude/settings.json` — hooks and all, from the pull
        # request under review — before any model call. Measured by the
        # adversarial review with a SessionStart hook that ran `touch`.
        "--setting-sources", "user",
        "--max-budget-usd", f"{cap_usd:.2f}",
    ]
    for d in dirs:
        argv += ["--add-dir", d]
    if model:
        argv += ["--model", model]
    return argv


def _claude_parse(stdout: str, out_dir: Path) -> tuple[str, float | None]:
    import json
    try:
        d = json.loads(stdout)
    except ValueError:
        return stdout, None
    cost = d.get("total_cost_usd")
    return str(d.get("result") or ""), float(cost) if isinstance(cost, (int, float)) else None


def _codex_argv(prompt: str, model: str, cap_usd: float, dirs: list[str]) -> list[str]:
    # Codex's own sandbox would try to nest inside this one; the cage is the
    # sandbox, so its own is off and its last message goes to out/.
    argv = ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "--output-last-message", "OUT/last.md"]
    if model:
        argv += ["-m", model]
    return argv + [prompt]


def _file_parse(name: str):
    def parse(stdout: str, out_dir: Path) -> tuple[str, float | None]:
        p = out_dir / name
        return (p.read_text(encoding="utf-8", errors="replace") if p.exists() else stdout), None
    return parse


def _plain_parse(stdout: str, out_dir: Path) -> tuple[str, float | None]:
    return stdout, None


def _gemini_argv(prompt: str, model: str, cap_usd: float, dirs: list[str]) -> list[str]:
    argv = ["gemini", "--yolo", "-p", prompt]
    return argv + (["-m", model] if model else [])


def _opencode_argv(prompt: str, model: str, cap_usd: float, dirs: list[str]) -> list[str]:
    argv = ["opencode", "run"]
    return argv + (["-m", model] if model else []) + [prompt]


ADAPTERS: dict[str, Adapter] = {
    "claude": Adapter(
        id="claude", label="Claude Code", binary="claude", verified=True,
        argv=_claude_argv, credentials=[".credentials.json"], config_env="CLAUDE_CONFIG_DIR",
        config_dirs=[".claude"], parse=_claude_parse, env_keys=["ANTHROPIC_API_KEY"],
    ),
    "codex": Adapter(
        id="codex", label="Codex", binary="codex", verified=False,
        argv=_codex_argv, credentials=["auth.json"], config_env="CODEX_HOME",
        config_dirs=[".codex"], parse=_file_parse("last.md"), env_keys=["OPENAI_API_KEY"],
    ),
    "gemini": Adapter(
        id="gemini", label="Gemini", binary="gemini", verified=False,
        argv=_gemini_argv, config_dirs=[".gemini"], parse=_plain_parse, env_keys=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    ),
    "opencode": Adapter(
        id="opencode", label="OpenCode", binary="opencode", verified=False,
        argv=_opencode_argv, config_dirs=[".config/opencode", ".local/share/opencode"], parse=_plain_parse,
    ),
}


def installed_adapters() -> list[Adapter]:
    return [a for a in ADAPTERS.values() if shutil.which(a.binary)]


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


@dataclass
class Cage:
    snapshot: Path
    out_dir: Path
    config_dir: Path | None
    argv: list[str]
    env: dict[str, str]


def _home_root(p: Path) -> Path | None:
    """The top of an install under HOME that `p` lives in — `~/.bun`,
    `~/.local/share/mise`, `~/.npm-global` — so the agent's runtime can be
    mounted back into an otherwise empty HOME without mounting all of it."""
    try:
        rel = p.resolve().relative_to(HOME.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] in (".local", ".config") and len(parts) > 1:
        depth = 3 if parts[1] in ("share", "state", "lib") and len(parts) > 2 else 2
        return HOME.resolve().joinpath(*parts[:depth])
    return HOME.resolve() / parts[0]


# Never mounted back, whatever PATH says: these hold credentials or history.
NEVER_MOUNT = {".ssh", ".gnupg", ".config/gh", ".config/agentglass", ".local/share/keyrings", ".aws", ".kube", ".docker"}


def runtime_mounts(adapter: Adapter, path: str | None = None) -> list[Path]:
    """What of HOME the agent needs to start: the installs its binary and
    PATH resolve into, and a version manager's config if one is in use."""
    roots: set[Path] = set()
    for entry in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if entry:
            r = _home_root(Path(entry))
            if r and r.exists():
                roots.add(r)
    exe = shutil.which(adapter.binary)
    if exe:
        r = _home_root(Path(exe))
        if r and r.exists():
            roots.add(r)
    mise = HOME / ".config" / "mise"
    if mise.is_dir():
        roots.add(mise.resolve())
    home = HOME.resolve()
    keep = []
    for r in sorted(roots):
        rel = str(r.relative_to(home)) if r.is_relative_to(home) else ""
        if rel and not any(rel == n or rel.startswith(n + "/") for n in NEVER_MOUNT):
            keep.append(r)
    return keep


def build(adapter: Adapter, snapshot: Path, extra_ro: list[Path], out_dir: Path, work: Path,
          prompt: str, model: str, cap_usd: float) -> Cage:
    """The bwrap command line for one run. Pure apart from preparing `work`,
    so what the cage mounts can be asserted without running anything."""
    bw = bwrap_path() or "bwrap"
    home = str(HOME.resolve())
    args: list[str] = [
        bw,
        # Its own pid, ipc, uts, cgroup and user namespaces: it cannot see or
        # signal a process outside. The network is kept (the model).
        "--unshare-all", "--share-net",
        "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        # An empty HOME: no history, no browser profiles, no tokens. The
        # agent's own install is mounted back below, read-only.
        "--tmpfs", home,
    ]
    for r in runtime_mounts(adapter):
        args += ["--ro-bind", str(r), str(r)]
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        args += ["--tmpfs", str(runtime)]
    # What the agent reads: the pull request at its head, and the context
    # gathered for it (diff, description, earlier threads). After the tmpfs
    # mounts, which would otherwise hide them.
    for p in [snapshot, *extra_ro]:
        args += ["--ro-bind", str(p), str(p)]

    env: dict[str, str] = {}
    config_dir: Path | None = None
    if adapter.config_env:
        # A fresh config directory: no settings, hooks, MCP servers or skills
        # of the user's that could run, and nothing written here survives.
        # Only the credential file is the real one, bound in place so a token
        # refresh lands in the real file rather than a copy that diverges.
        config_dir = work / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(config_dir), str(config_dir)]
        real = HOME / adapter.config_dirs[0] if adapter.config_dirs else None
        for cred in adapter.credentials:
            src = real / cred if real else None
            if src and src.is_file():
                dst = config_dir / cred
                dst.touch()
                args += ["--bind", str(src), str(dst)]
        env[adapter.config_env] = str(config_dir)
    else:
        # Without a variable to redirect it, the agent's own config is mounted
        # back read-only. Some CLIs refuse to start that way; that is the
        # honest failure, and the reason they are marked experimental.
        for d in adapter.config_dirs:
            p = HOME / d
            if p.is_dir():
                args += ["--ro-bind", str(p), str(p)]

    out_dir.mkdir(parents=True, exist_ok=True)
    args += ["--bind", str(out_dir), str(out_dir), "--chdir", str(snapshot)]

    # The environment is an allowlist, not the process's minus a denylist.
    args += ["--clearenv"]
    for k in ENV_ALLOW + adapter.env_keys:
        v = os.environ.get(k)
        if v:
            env[k] = v
    env.update({"HOME": home, "TERM": "dumb", "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"})
    for k, v in env.items():
        args += ["--setenv", k, v]
    argv = [a.replace("OUT/", str(out_dir) + "/") for a in adapter.argv(prompt, model, cap_usd, [str(p) for p in extra_ro])]
    return Cage(snapshot=snapshot, out_dir=out_dir, config_dir=config_dir, argv=args + argv, env=dict(os.environ))


def self_test(agentglass_url: str | None = None, adapter: Adapter | None = None) -> dict[str, object]:
    """The cage, checked on this machine: what an agent would find if it
    tried. No model is called; cheap enough to run on every start.

    `agentglass_url` adds the loopback check (does agentglass answer inside
    the cage without a token?), `adapter` the check that the agent can start
    in an empty HOME at all."""
    import subprocess
    bw = bwrap_path()
    if not bw:
        return {"ok": False, "why": "bubblewrap (bwrap) is not installed — install it to run reviews", "checks": {}}
    with tempfile.TemporaryDirectory(prefix="agx-cage-test-") as t:
        snap, out, work = Path(t, "snap"), Path(t, "out"), Path(t, "work")
        snap.mkdir()
        (snap / "a.txt").write_text("x")
        probe = Adapter(id="probe", label="probe", binary="bash", verified=True,
                        argv=lambda p, m, c, d: ["bash", "-c", p], parse=_plain_parse)
        url = (agentglass_url or "").rstrip("/")
        script = (
            "w=no; touch x 2>/dev/null && w=yes; "
            "touch \"$HOME/.agx-cage-probe\" 2>/dev/null; "
            "g=no; command -v gh >/dev/null && gh auth status >/dev/null 2>&1 && g=yes; "
            "s=$(ls -A \"$HOME/.ssh\" 2>/dev/null | wc -l); "
            f"k=no; kill -0 {os.getpid()} 2>/dev/null && k=yes; "
            "e=no; [ -n \"$AGX_CANARY\" ] && e=yes; "
            "o=no; touch OUT/ok 2>/dev/null && o=yes; "
            + (f"a=$(python3 -c 'import urllib.request,sys\ntry:\n urllib.request.urlopen(\"{url}/plugins\",timeout=3);print(200)\n"
               "except urllib.error.HTTPError as x:print(x.code)\nexcept Exception:print(0)' 2>/dev/null); " if url else "a=skip; ")
            + "echo \"$w $g $s $k $e $o $a\""
        )
        cage = build(probe, snap, [], out, work, script.replace("OUT/", str(out) + "/"), "", 0)
        try:
            r = subprocess.run(cage.argv, env={**cage.env, "AGX_CANARY": "leak"}, capture_output=True, text=True,
                               errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "why": f"bwrap did not run: {e}", "checks": {}}
        parts = r.stdout.split()
        if len(parts) != 7:
            return {"ok": False, "why": f"bwrap could not start a cage here: {r.stderr.strip()[:200]}", "checks": {}}
        wrote_snap, gh_in, ssh_n, saw_host, env_leak, wrote_out, api = parts
        leak = HOME / ".agx-cage-probe"
        home_leak = leak.exists()
        if home_leak:
            leak.unlink()
        checks = {
            "the pull request's code is read-only": wrote_snap == "no",
            "nothing written to HOME survives": not home_leak,
            "GitHub CLI has no login inside": gh_in == "no",
            "no SSH keys inside": ssh_n == "0",
            "host processes are out of reach": saw_host == "no",
            "the environment is an allowlist": env_leak == "no",
            "only the output directory is writable": wrote_out == "yes",
        }
        if api != "skip":
            # 200 means agentglass is on loopback with no token: anything in
            # the cage would have its whole API. 401 is the right answer.
            checks["agentglass needs a token from inside"] = api != "200"
        if adapter is not None:
            ok, _why = agent_starts(adapter)
            checks[f"{adapter.label} starts in an empty HOME"] = ok
        failed = [k for k, v in checks.items() if not v]
        return {"ok": not failed, "checks": checks,
                "why": None if not failed else "the cage does not hold: " + ", ".join(failed)}


def agent_starts(adapter: Adapter) -> tuple[bool, str]:
    """`<agent> --version` inside the cage, with its throwaway config. A CLI
    that needs more of HOME than its install fails here, before a review
    spends anything on it."""
    import subprocess
    if not shutil.which(adapter.binary):
        return False, f"{adapter.binary} is not installed"
    with tempfile.TemporaryDirectory(prefix="agx-cage-agent-") as t:
        snap = Path(t, "snap"); snap.mkdir()
        probe = Adapter(**{**adapter.__dict__, "argv": lambda p, m, c, d: [adapter.binary, "--version"]})
        cage = build(probe, snap, [], Path(t, "out"), Path(t, "work"), "", "", 0)
        try:
            r = subprocess.run(cage.argv, env=cage.env, capture_output=True, text=True, errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]
