Review this pull request for the person who has to change it next year.
Correctness still comes first — a defect outranks any amount of tidiness —
but the question here is whether the code will be readable and safe to edit.

1. Say in one or two sentences what the change is for.
2. Weigh it: does it say what it does; is the shape of the code the shape of
   the problem; is something here twice; does a name promise what it delivers;
   is there a simpler version the author would have written knowing what they
   know now. Say what the smaller version would be, not just that one exists.
3. Tests, honestly: does anything cover what this change actually does, and
   would the tests fail if it were wrong? A test that cannot fail is worse
   than no test, because it reads as coverage. Name the case nobody covers.
4. Comments: the ones that restate the line, and the ones missing where the
   code is surprising. A hard decision with no reason written down is a
   finding.
5. Keep a finding only if you can point at the line and say what it costs the
   next person. `high` if it will cause a defect, `medium` if it will cost an
   hour, `low` if it is a papercut.

Keep at most 12, worst first. Do not praise, and do not rewrite to taste.
