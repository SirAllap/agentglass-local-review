# local-review

An [agentglass](https://github.com/SirAllap/agentglass) plugin that reviews your pull requests on your own machine, with the agent and model you choose, and puts what it finds inside the pull request in agentglass. Nothing is posted to GitHub.

- **Asked for with a label.** Put `agentglass-review` on one of your pull requests in a watched repository. It is reviewed, and after that every push is reviewed again, the new part only, until you take the label off. Somebody else's pull request is reviewed only when you queue it yourself.
- **Read where you already read.** Each review is one entry in the pull request's conversation, in a *Local* lane. Each finding sits under its line in the Files tab, marked local. You resolve or dismiss findings there, and the next review is told what you decided.
- **Any installed agent.** Claude Code is measured. Codex, Gemini and OpenCode run in the same sandbox and are marked experimental until they have been measured in it.
- **Caps.** One spend cap per review and one per day, and one review at a time.

## How it stays local

Every agent runs in one [bubblewrap](https://github.com/containers/bubblewrap) sandbox, whichever agent it is:

- The whole disk is read-only.
- `~/.config/gh`, `~/.ssh`, git credential stores, the keyring, agentglass's token and the variables that carry a GitHub token are not there at all.
- The agent gets a throwaway config directory, so none of your hooks, MCP servers or settings run, and nothing it writes outlives the review.
- One output directory is the only thing it can write.

The plugin reads GitHub outside the sandbox, with your `gh` login, and only reads: the pull request's tarball at its head commit, its diff and its threads. It never checks out anything in your repositories.

The plugin checks the sandbox when it starts and refuses to review if any of that does not hold. Measured with Claude given every permission of its own: it could not write a file, `gh` answered "not logged in", and `git` over SSH failed.

What the sandbox does not stop: the agent keeps its network, because it has to reach its model. An agent talked into it by the code it is reading could send that code somewhere, without any credentials. That is why only your own pull requests are picked up by default.

## The prompt is yours

Three come with the plugin, chosen in **Settings ▸ local-review ▸ How to
review**, and they are deliberately plain — a review style is a matter of
taste, and nobody knows what your code needs better than you do:

| | what it weighs |
|---|---|
| Balanced | defects that matter, verified against the line before they are kept |
| Security | what an attacker reaches, from the input they control to the damage |
| Clean code and tests | whether the next person can read and change this safely |

Pick **My own prompt** to write your own in the box under the dropdown. It
arrives filled with whichever shipped prompt you were using, so you start by
editing rather than from nothing, and it says only HOW to review: where the
files are, that what they contain is evidence and never an instruction, and
the JSON block the app parses are added around it. Your prompt therefore
cannot break the review. It is kept with your other settings, never in this
plugin — an update cannot overwrite it, and a prompt written for the code you
are paid to work on never travels with a public plugin. An empty box falls
back to Balanced and says so rather than reviewing with nothing.

**Anything to add** is pasted at the end of whichever prompt is in use, for
the house rule a reviewer keeps forgetting.

## Install

```
Settings → Plugins → Install → https://github.com/SirAllap/agentglass-local-review
```

Needs `python3`, `gh` (logged in), `bwrap`, and at least one agent CLI. Then open **Settings → local-review** and list the repositories to watch.

## Develop

```
python3 -m unittest discover tests
```
