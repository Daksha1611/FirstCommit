"""The buyer graph: builder, state, routing, reducers.

Layout follows RabbitHole's courtroom/graph/ - assembly, state shape, routing
rules and merge functions each in their own module, so none of them has to be
read to understand another.
"""

from .builder import MAX_ROUNDS, Buyer, ErrandResult, Stage, build_buyer, run_errand
from .entry import Intent, NotSupported, interpret, register_standing
from .reducers import merge_by_key, merge_proposals
from .route import classify, route_after_refusal
from .state import ErrandAction, ErrandState, ProposalState, RefusalState

__all__ = [
    "MAX_ROUNDS", "Buyer", "ErrandResult", "Stage", "build_buyer", "run_errand",
    "interpret", "register_standing", "Intent", "NotSupported",
    "merge_by_key", "merge_proposals",
    "classify", "route_after_refusal",
    "ErrandAction", "ErrandState", "ProposalState", "RefusalState",
]
