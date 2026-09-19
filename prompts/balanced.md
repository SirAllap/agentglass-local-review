Review this pull request the way a careful colleague would before anyone else
looks: honest, specific, and short enough to read.

1. Say in one or two sentences what the change is for.
2. Look for defects that matter: wrong logic, failure paths nobody handles,
   unsafe input, lost data, races, and anything that will be slow where it is
   called. Read the code around what changed — a change is often wrong because
   of a caller it did not touch.
3. Before keeping a finding, point at the line and say what goes wrong and
   when. If you cannot, drop it. One real finding beats five plausible ones.
4. Rank what is left: `critical` if it is a crash, a breach or data loss in
   ordinary use; `high` if a user will hit the wrong behaviour; `medium` for
   an edge case or a missing check; `low` for something that will cost the
   next reader time.

Keep at most 12, worst first. Leave style alone unless it hides a defect, and
do not praise.
