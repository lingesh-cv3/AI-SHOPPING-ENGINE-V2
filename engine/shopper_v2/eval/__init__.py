"""Stage 2A evaluation infrastructure for Shopper V2.

Everything under this package is measurement-only: it drives the EXISTING,
unmodified retrieval/evidence pipeline (`engine/shopper_v2/retrieval.py`,
`engine/shopper_v2/evidence.py`, and the V1 deterministic helpers they call
in `engine/execution/relevance.py` / `engine/execution/mission_ranking.py`)
against a fixed corpus and records what actually happens - it does not
change what that pipeline does, add a new retrieval method, or add a new
relevance heuristic. See SHOPPER_V2_STAGE2_SPEC.md §18 (Stage 2A) and
SHOPPER_V2_BASELINE_REPORT.md for the results this package produced.

Deliberately does not import `engine.reasoning` for anything other than the
type used by Stage 1's own `orchestrator.run_turn` signature: the
corpus-driven baseline builds `SearchIntent` directly from each eval case's
own stated, ground-truth fields (query/budget/exclusions/priorities),
bypassing the LLM `understand()` step entirely. This is a deliberate
Stage 2A design decision, not an oversight - see `runner.py`'s own
docstring for why.
"""
