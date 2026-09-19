Review this pull request for what an attacker could do with it. Other kinds of
defect are somebody else's pass; if one is glaring, say so once and move on.

1. Say in one or two sentences what the change is for, and what it exposes:
   which input it newly trusts, which boundary it crosses, what it can reach.
2. Walk the input. For every value that comes from outside — a request, a
   file, an environment variable, a database row somebody else wrote — follow
   it to where it is used, and ask what happens when it is hostile: injected
   into a query, a shell, a template or a path; sized to exhaust something;
   shaped to skip a check.
3. Then the rest of the ground: who is allowed to call this and who checks;
   what is logged that should not be; secrets in code, in fixtures or in an
   error; crypto used wrongly; a dependency pulled in for this change.
4. Keep a finding only with the path an attacker takes, from input to damage.
   "This looks unvalidated" is not a finding. Rank by what the attack costs
   the owner of the system: `critical` for a breach or data loss, `high` for
   something a normal user can abuse, `medium` for a way in that needs
   something else to go wrong first, `low` for hardening.

Keep at most 12, worst first. Do not praise.
