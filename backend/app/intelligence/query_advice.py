"""Safer-query suggestions built from the allowlisted schema.

Suggestions are structured guidance, not rewritten SQL: a rewrite would carry
the caller's literals into a retained decision. Any query the operator writes
from a suggestion is a new operation and is analysed from scratch.
"""

from __future__ import annotations

from .internal import QueryRecommendation
from .policy import QueryPolicyConfig
from .sql_ast import QueryStructure, SqlAllowlist

_OUTPUT_RULES = {"OUT-001", "OUT-002", "OUT-003", "OUT-004"}
_HARD_OUTPUT_RULES = {"OUT-001", "OUT-002", "OUT-003"}


def _rec(template_id: str, summary: str, changes: bool, caveats: list[str]) -> QueryRecommendation:
    return QueryRecommendation(template_id=template_id, summary=summary, changes_result_set=changes, caveats=tuple(caveats))


def rejection_recommendations(rule_id: str) -> list[QueryRecommendation]:
    return [
        _rec(
            "REC-SUPPORTED-SUBSET",
            "Rewrite as a single read-only SELECT over allowlisted tables, columns and functions; "
            "see docs/intelligence/README.md for the supported grammar.",
            True,
            ["The guard rejects unknown syntax rather than guessing it is safe.", f"Rejected by {rule_id}."],
        )
    ]


def recommend(
    structure: QueryStructure,
    rule_ids: tuple[str, ...],
    allow: SqlAllowlist,
    config: QueryPolicyConfig,
) -> list[QueryRecommendation]:
    rules = set(rule_ids)
    out: list[QueryRecommendation] = []
    primary = allow.tables[structure.tables[0]] if structure.tables else None
    indexed = sorted(primary.indexed_columns) if primary else []
    key = primary.primary_key if primary and primary.primary_key else (indexed[0] if indexed else None)

    if rules & _OUTPUT_RULES and structure.select_star:
        out.append(
            _rec(
                "REC-PROJECT-COLUMNS",
                "Select only the columns you need instead of '*'.",
                True,
                ["Reduces bytes per row, not the number of rows.", "Returns fewer columns than the original."],
            )
        )
    if "OUT-003" in rules or ({"OUT-001", "OUT-002"} & rules and structure.has_limit is False):
        if key:
            out.append(
                _rec(
                    "REC-KEYSET-PAGINATION",
                    f"Page through results with keyset pagination on indexed key '{key}': filter "
                    f"'{key} > $last_seen', ORDER BY '{key}', LIMIT {config.interactive_max_rows}.",
                    True,
                    [
                        "Returns one page, not the full result; each further page is a new approved request.",
                        "Ordering by an indexed key lets LIMIT stop early instead of sorting everything.",
                    ],
                )
            )
        out.append(
            _rec(
                "REC-ADD-LIMIT",
                f"Add LIMIT {config.interactive_max_rows} or less for an interactive preview.",
                True,
                ["Without ORDER BY, which rows are returned is unspecified."],
            )
        )
    if rules & _OUTPUT_RULES and not structure.has_where and indexed:
        out.append(
            _rec(
                "REC-INDEXED-FILTER",
                "Narrow the query with a filter on an indexed column (" + ", ".join(indexed) + ").",
                True,
                ["Changes which rows are returned; confirm the filter matches the intent."],
            )
        )
    if rules & _HARD_OUTPUT_RULES:
        out.append(
            _rec(
                "REC-BOUNDED-EXPORT",
                "If full-table processing is genuinely needed, request an approved bounded batch export "
                "instead of the interactive console.",
                False,
                ["A full scan is not wrong in itself; the interactive console's response budget is the limit."],
            )
        )
    if "SORT-001" in rules:
        out.append(
            _rec(
                "REC-SORT-ON-INDEX",
                "Order by an indexed column" + (f" ({', '.join(indexed)})" if indexed else "")
                + " so the plan can avoid a full sort.",
                True,
                ["LIMIT alone does not make an explicit sort cheap: the whole input is sorted first."],
            )
        )
    if "COST-001" in rules and structure.aggregate_only:
        out.append(
            _rec(
                "REC-AGGREGATE-SCAN",
                "The aggregate returns one row but still scans its input; add an indexed filter if a subset suffices.",
                True,
                ["A one-row result is a small output risk but not necessarily a cheap plan."],
            )
        )
    if {"STATE-002", "STATE-003", "STATE-004"} & rules:
        out.append(
            _rec(
                "REC-RETRY-LATER",
                "Retry when database load is lower or the active experiment has finished.",
                False,
                ["Resubmission is analysed again against fresh state."],
            )
        )
    return out
