"""Prompts as modules, following RabbitHole's layout.

Kept out of the node files so a prompt can be edited, diffed and reviewed on its
own. In RabbitHole judiciary_prompt.py is 585 lines; the prompt is the artefact,
not a string literal hiding inside a function.
"""

from .broker_prompt import BROKER_INSTRUCTION, SUBPAYER_INSTRUCTION
from .chooser_prompt import CHOOSER_INSTRUCTION
from .intake_prompt import INTAKE_CLASSIFIER_PROMPT, INTAKE_HUMAN
from .planner_prompt import PLANNER_PROMPT
from .payer_prompt import PAYER_INSTRUCTION
from .reviewer_prompt import REVIEWER_INSTRUCTION
from .triage_prompt import TRIAGE_PROMPT
from .standing_prompt import STANDING_PROMPT
from .shopper_prompt import STRATEGY_BRIEFS, shopper_instruction

__all__ = [
    "BROKER_INSTRUCTION",
    "SUBPAYER_INSTRUCTION",
    "CHOOSER_INSTRUCTION",
    "INTAKE_CLASSIFIER_PROMPT",
    "INTAKE_HUMAN",
    "PAYER_INSTRUCTION",
    "PLANNER_PROMPT",
    "REVIEWER_INSTRUCTION",
    "STRATEGY_BRIEFS",
    "STANDING_PROMPT",
    "TRIAGE_PROMPT",
    "shopper_instruction",
]

from agent.prompts.decompose_prompt import (  # noqa: E402,F401
    DECOMPOSE_PROMPT,
    DECOMPOSE_HUMAN,
)

from agent.prompts.critic_prompt import CRITIC_PROMPT  # noqa: E402,F401
