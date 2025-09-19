"""Turning a standing instruction into a reorder policy.

"Keep the stationery cupboard stocked" is a sentence. A reorder policy is a set
of numbers: which SKUs, at what level to reorder, up to what level.

This prompt runs ONCE, when the instruction is set up. Every tick afterwards is
arithmetic against those numbers - below the reorder point, buy up to target -
and needs no model at all. That matters for a thing designed to run
unattended for months: re-interpreting the same sentence every week is both
wasteful and a way for behaviour to drift without anyone changing anything.
"""

STANDING_PROMPT = """\
You are setting up a standing purchase policy at {company}. Amounts are in
paise; 100 paise is one rupee.

Someone has given a recurring instruction. Turn it into reorder rules that can
run unattended.

THE INSTRUCTION
  {instruction}

DEPARTMENT   {department}
BUDGET       {budget_paise} paise per {period}
CATALOGUE
{catalogue}

FOR EACH SKU THE INSTRUCTION COVERS, DECIDE
  reorder_point   the level at which topping up should begin
  target_level    the level to top up to

Set them so a normal month's use does not run the item to zero, and so the
cupboard does not fill with stock nobody asked for. A reorder point of zero
means the item runs out before anything happens; a target far above use means
money sitting on a shelf.

ONLY include SKUs the instruction actually covers. "Keep the stationery
cupboard stocked" does not authorise buying rack servers, and this policy will
run for months without anyone reading it again.

Say what you assumed - typical consumption, headcount, anything you inferred
that was not stated.
"""
