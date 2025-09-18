// Authorisation policy for the money path.
//
// This file is deliberately short, and the shortness is the point.
//
// Almost all of this system's authority is cryptographic: what a token may do
// is fixed when it is minted, attenuation only ever narrows, and no wording an
// agent chooses can widen it. None of that belongs here - a policy engine that
// re-decides what a signature already settled is a second opinion on a fact.
//
// What belongs here is the small set of rules that are *chosen*. The money path
// has always distinguished the two in its own comments: "the per-seller caps
// below are a cryptographic guarantee; this line is a rule we chose to apply
// here." Those chosen lines were `if` statements at line 1173 and line 1523 of
// a two-thousand-line module. They are policy, they are the part a reviewer
// most needs to read, and they were the hardest part of the system to find.
//
// Now they are four lines someone can be shown.
//
// Cedar evaluates forbid before permit and a forbid always wins, so each rule
// below is a carve-out from the final permit rather than an ordering puzzle.

// A broker may delegate, not spend.
//
// The broker's own token necessarily also permits `pay`: attenuation is
// monotonic, and a token cannot confer a capability it does not hold, so a
// broker that can grant `pay` to its sub-payers must carry `pay` itself. The
// token language cannot express "may grant but not exercise". This is that
// missing sentence, and it is enforced on identity at the gateway.
@id("broker-may-not-spend")
forbid (
    principal,
    action == Action::"pay",
    resource
)
when { principal has role && principal.role == "broker" };

// Payout is off on this deployment.
//
// No token this gateway mints carries `tool:payout` in scope, so a payout is
// already refused one step earlier for want of authority. This rule exists
// because that is true *today*: a path that cannot be reached now is exactly
// the path that is reached after someone widens a scope next month. Written
// closed, in the file where a person would look for it, rather than as an
// unreachable branch at the bottom of a long function.
@id("payout-is-not-enabled")
forbid (
    principal,
    action == Action::"payout",
    resource
)
unless { context.payout_enabled };

// Everything that reaches this point has already been authenticated, verified
// against its chain, checked for expiry and depth, and reserved against a
// cumulative budget. This permit is the statement that policy adds nothing
// further - not that nothing was checked.
@id("permitted-after-cryptographic-checks")
permit (
    principal,
    action,
    resource
);
