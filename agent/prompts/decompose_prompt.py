"""The one question a model is asked per branch node of the funnel.

Everything else in pocketchange/funnel.py - allocating budgets, appending
delegation blocks, checking the six bounds - is arithmetic. This is the single
judgement, and it is asked once per branch rather than once per node: 40 calls
for a 121-node tree, not 121.

The prompt is deliberately narrow. It does not choose SKUs, name prices, or
decide whether something is affordable; those have deterministic answers
elsewhere and a model asked for them will produce plausible ones. It is asked
only how a piece of work divides.
"""

DECOMPOSE_PROMPT = """\
You are splitting one procurement task into smaller independent sub-tasks.

THE TASK
  {description}

WHAT IT HAS
  budget      {budget_rupees} rupees
  depth       {depth} of {max_depth}
  sourcing    {sourcing}

YOUR JOB IS TO DIVIDE IT
Return 2 to {max_fanout} sub-tasks. Most procurement tasks divide readily: by
product category, by team or department, by delivery phase, or by supplier type.
A task carrying a budget this size is almost always several purchases wearing one
sentence.

WHEN NOT TO DIVIDE
Return an empty list ONLY if this is literally one line item from one supplier -
"a box of A4 paper", "one replacement keyboard". If you can name two things that
would arrive in two different boxes, it divides. Do not return empty because the
task looks tidy, or because you are unsure how to split it; unsure means divide
by product category.

RULES
1. Every sub-task must be strictly smaller in scope than the task above. Never
   restate the task in different words: a sub-task that means the same thing as
   its parent is a loop and will be refused.
2. The budget shares must sum to AT MOST {budget_rupees} rupees. They may sum to
   less. They may never sum to more.
3. Sourcing. LEAVE IT UNSET on every sub-task unless you are narrowing it.
   Unset means the sub-task is sourced the way its parent is, which is almost
   always right: the person chose the parent's sourcing and it should reach the
   purchase they chose it for.
     catalogue  our own catalogue. No external text at all.
     specific   one named supplier. You must name it; never invent one.
     best       find the source by searching the open web.
   You may only narrow: catalogue is narrower than specific, which is narrower
   than best. A wider value is clamped back to the parent's and ignored, so
   asking for `best` under a catalogue task achieves nothing.
4. You are dividing work. You are not choosing products, quoting prices, or
   deciding what is affordable. Do not do those things here.
"""

DECOMPOSE_HUMAN = """\
Divide the task above into 2 to {max_fanout} sub-tasks with budget shares.
Return an empty list only if it is genuinely one line item from one supplier.
"""
