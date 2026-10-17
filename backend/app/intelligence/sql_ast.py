"""Default-deny AST validation for the guarded SQL console.

Supported grammar (``SQL_GRAMMAR_VERSION``): one plain ``SELECT`` statement with
  * FROM: allowlisted tables, optionally aliased, combined with INNER/LEFT JOIN
    (ON or USING). At least one table is required.
  * Target list: allowlisted columns, ``*`` / ``alias.*``, allowlisted scalar
    functions and aggregates, COALESCE, casts to allowlisted types, constants.
  * WHERE / HAVING: comparison, arithmetic, IN (value list), BETWEEN,
    LIKE/ILIKE, IS [NOT] NULL, AND/OR/NOT.
  * GROUP BY, ORDER BY (columns, output aliases, ordinals), plain DISTINCT.
  * LIMIT / OFFSET as integer constants or ``$n`` parameters.

Everything else is rejected, including CTEs, sub-queries, set operations,
window functions, aggregate FILTER/ORDER, SELECT INTO, row locks, DISTINCT ON,
FETCH ... WITH TIES, table functions and TABLESAMPLE. Parameters are bound by
the database driver; nothing here substitutes values into SQL text.

Rejection reasons are fixed strings. Parser messages are never forwarded
because they can echo fragments of the submitted SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Iterable, Sequence

from pglast import ast, parse_sql
from pglast.enums import A_Expr_Kind, BoolExprType, JoinType, LimitOption, SetOperation
from pglast.parser import ParseError

SQL_GRAMMAR_VERSION = "select-subset-1"

DEFAULT_SCALAR_FUNCTIONS = frozenset(
    {"lower", "upper", "length", "abs", "round", "floor", "ceil", "date_trunc", "greatest", "least"}
)
DEFAULT_AGGREGATES = frozenset({"count", "sum", "avg", "min", "max"})
DEFAULT_OPERATORS = frozenset(
    {"=", "<>", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "~~", "~~*", "!~~", "!~~*"}
)
DEFAULT_CAST_TYPES = frozenset(
    {"int2", "int4", "int8", "numeric", "float8", "text", "varchar", "date", "timestamp", "timestamptz", "bool"}
)
ALLOWED_PARAMETER_TYPES = (str, int, float, bool, type(None))


class SqlRejected(Exception):
    """Validation failure with a stable rule ID and a reason that never echoes SQL."""

    def __init__(self, rule_id: str, reason: str):
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


@dataclass(frozen=True)
class TableSpec:
    columns: frozenset[str]
    indexed_columns: frozenset[str] = frozenset()
    primary_key: str | None = None


@dataclass(frozen=True)
class SqlAllowlist:
    tables: Mapping[str, TableSpec]  # keyed "schema.table"
    default_schema: str
    scalar_functions: frozenset[str] = DEFAULT_SCALAR_FUNCTIONS
    aggregates: frozenset[str] = DEFAULT_AGGREGATES
    operators: frozenset[str] = DEFAULT_OPERATORS
    cast_types: frozenset[str] = DEFAULT_CAST_TYPES
    max_sql_bytes: int = 8192
    max_parameters: int = 32

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SqlAllowlist":
        tables = {
            name: TableSpec(
                columns=frozenset(spec["columns"]),
                indexed_columns=frozenset(spec.get("indexed_columns", [])),
                primary_key=spec.get("primary_key"),
            )
            for name, spec in data["tables"].items()
        }
        return cls(tables=tables, default_schema=data["default_schema"])


@dataclass(frozen=True)
class QueryStructure:
    """Sanitized structural summary. Contains schema identifiers, never literals."""

    tables: tuple[str, ...]
    n_joins: int
    n_predicates: int
    n_aggregates: int
    n_projected_columns: int
    n_functions: int
    n_params: int
    select_star: bool
    has_where: bool
    has_limit: bool
    limit_is_param: bool
    has_offset: bool
    has_order_by: bool
    has_group_by: bool
    has_distinct: bool
    aggregate_only: bool
    filters_on_indexed: bool
    order_by_indexed: bool


@dataclass
class ValidatedQuery:
    """TRANSIENT. Holds the raw SQL for the request path only; never log or serialize."""

    sql: str = field(repr=False)
    structure: QueryStructure
    limit_constant: int | None = field(default=None, repr=False)
    parameters: list[Any] = field(default_factory=list, repr=False)  # positional, transient


# ---------------------------------------------------------------------------


_SELECT_ALLOWED_ATTRS = {
    "targetList",
    "fromClause",
    "whereClause",
    "groupClause",
    "havingClause",
    "sortClause",
    "limitCount",
    "limitOffset",
    "limitOption",
    "op",
    "all",
    "groupDistinct",
    "distinctClause",
}


def _is_empty(value: Any) -> bool:
    return value is None or value is False or value == () or value == []


def _strings(nodes: Iterable[Any]) -> list[str]:
    out = []
    for n in nodes:
        if not isinstance(n, ast.String):
            raise SqlRejected("SQL-014", "unsupported identifier form")
        out.append(n.sval)
    return out


class _Walker:
    def __init__(self, allow: SqlAllowlist):
        self.allow = allow
        self.scope: dict[str, str] = {}  # alias -> "schema.table"
        self.output_aliases: set[str] = set()
        self.n_joins = 0
        self.n_predicates = 0
        self.n_aggregates = 0
        self.n_functions = 0
        self.max_param = 0
        self.select_star = False
        self.projected = 0

    # -- statement ---------------------------------------------------------

    def select(self, stmt: ast.SelectStmt) -> tuple[QueryStructure, int | None]:
        if stmt.withClause is not None:
            raise SqlRejected("SQL-005", "common table expressions are not supported")
        if stmt.intoClause is not None:
            raise SqlRejected("SQL-006", "SELECT INTO is not permitted")
        if stmt.lockingClause:
            raise SqlRejected("SQL-007", "row locking clauses are not permitted")
        if stmt.op != SetOperation.SETOP_NONE or stmt.larg is not None or stmt.rarg is not None:
            raise SqlRejected("SQL-008", "set operations are not supported")
        for slot in type(stmt).__slots__:
            if slot not in _SELECT_ALLOWED_ATTRS and not _is_empty(getattr(stmt, slot, None)):
                raise SqlRejected("SQL-014", "unsupported SELECT clause")
        if stmt.limitOption not in (LimitOption.LIMIT_OPTION_DEFAULT, LimitOption.LIMIT_OPTION_COUNT):
            raise SqlRejected("SQL-014", "only plain LIMIT is supported")
        has_distinct = False
        if stmt.distinctClause:
            if tuple(stmt.distinctClause) != (None,):
                raise SqlRejected("SQL-014", "DISTINCT ON is not supported")
            has_distinct = True
        if not stmt.fromClause:
            raise SqlRejected("SQL-014", "a FROM clause over an allowlisted table is required")
        if len(stmt.fromClause) != 1:
            raise SqlRejected("SQL-014", "comma-separated FROM lists are not supported; use JOIN ... ON")

        for item in stmt.fromClause:
            self._from_item(item)

        all_aggregate = True
        for target in stmt.targetList or ():
            if not isinstance(target, ast.ResTarget) or target.indirection:
                raise SqlRejected("SQL-014", "unsupported target list entry")
            if target.name:
                self.output_aliases.add(target.name)
            val = target.val
            if isinstance(val, ast.ColumnRef) and isinstance(val.fields[-1], ast.A_Star):
                self._star(val)
                all_aggregate = False
                continue
            before = self.n_aggregates
            self._expr(val, allow_aggregates=True)
            self.projected += 1
            if not (isinstance(val, ast.FuncCall) and self.n_aggregates > before):
                all_aggregate = False

        top_conjuncts: list[Any] = []
        if stmt.whereClause is not None:
            self._expr(stmt.whereClause, allow_aggregates=False)
            top_conjuncts = self._conjuncts(stmt.whereClause)
        for g in stmt.groupClause or ():
            self._expr(g, allow_aggregates=False)
        if stmt.havingClause is not None:
            self._expr(stmt.havingClause, allow_aggregates=True)

        order_by_indexed = False
        for i, sort in enumerate(stmt.sortClause or ()):
            if not isinstance(sort, ast.SortBy) or sort.useOp:
                raise SqlRejected("SQL-014", "unsupported ORDER BY form")
            node = sort.node
            if isinstance(node, ast.A_Const) and isinstance(node.val, ast.Integer):
                continue
            if isinstance(node, ast.ColumnRef) and len(node.fields) == 1 and isinstance(node.fields[0], ast.String) and node.fields[0].sval in self.output_aliases:
                continue
            self._expr(node, allow_aggregates=True)
            if i == 0 and isinstance(node, ast.ColumnRef):
                table, column = self._resolve(node)
                order_by_indexed = column in self.allow.tables[table].indexed_columns

        limit_constant, limit_is_param = self._limit_value(stmt.limitCount)
        self._limit_value(stmt.limitOffset)

        structure = QueryStructure(
            tables=tuple(sorted(set(self.scope.values()))),
            n_joins=self.n_joins,
            n_predicates=self.n_predicates,
            n_aggregates=self.n_aggregates,
            n_projected_columns=self.projected,
            n_functions=self.n_functions,
            n_params=self.max_param,
            select_star=self.select_star,
            has_where=stmt.whereClause is not None,
            has_limit=stmt.limitCount is not None,
            limit_is_param=limit_is_param,
            has_offset=stmt.limitOffset is not None,
            has_order_by=bool(stmt.sortClause),
            has_group_by=bool(stmt.groupClause),
            has_distinct=has_distinct,
            aggregate_only=all_aggregate and not stmt.groupClause and bool(stmt.targetList),
            filters_on_indexed=any(self._indexed_filter(c) for c in top_conjuncts),
            order_by_indexed=order_by_indexed,
        )
        return structure, limit_constant

    # -- FROM ----------------------------------------------------------------

    def _from_item(self, item: Any) -> None:
        if isinstance(item, ast.RangeVar):
            if item.catalogname:
                raise SqlRejected("SQL-009", "catalog-qualified relations are not permitted")
            schema = item.schemaname or self.allow.default_schema
            key = f"{schema}.{item.relname}"
            if key not in self.allow.tables:
                raise SqlRejected("SQL-009", "relation is not on the allowlist")
            alias = item.relname
            if item.alias is not None:
                if item.alias.colnames:
                    raise SqlRejected("SQL-014", "column aliases on relations are not supported")
                alias = item.alias.aliasname
            if alias in self.scope:
                raise SqlRejected("SQL-014", "duplicate relation alias")
            self.scope[alias] = key
            return
        if isinstance(item, ast.JoinExpr):
            if item.jointype not in (JoinType.JOIN_INNER, JoinType.JOIN_LEFT) or item.isNatural:
                raise SqlRejected("SQL-014", "only INNER and LEFT joins are supported")
            if item.alias is not None or item.join_using_alias is not None:
                raise SqlRejected("SQL-014", "join aliases are not supported")
            if item.quals is None and not item.usingClause:
                raise SqlRejected("SQL-014", "joins need an ON or USING condition (no cross joins)")
            self._from_item(item.larg)
            self._from_item(item.rarg)
            self.n_joins += 1
            if item.usingClause:
                for col in _strings(item.usingClause):
                    if not any(col in self.allow.tables[t].columns for t in self.scope.values()):
                        raise SqlRejected("SQL-010", "column is not on the allowlist")
            if item.quals is not None:
                self._expr(item.quals, allow_aggregates=False)
                if not any(self._column_equality(c) for c in self._conjuncts(item.quals)):
                    raise SqlRejected("SQL-014", "join ON must include an equality between two columns")
            return
        raise SqlRejected("SQL-014", "unsupported FROM item")

    # -- expressions ---------------------------------------------------------

    def _expr(self, node: Any, *, allow_aggregates: bool) -> None:
        if isinstance(node, ast.ColumnRef):
            self._resolve(node)
        elif isinstance(node, ast.A_Const):
            if not node.isnull and not isinstance(node.val, (ast.Integer, ast.Float, ast.String, ast.Boolean)):
                raise SqlRejected("SQL-014", "unsupported constant")
        elif isinstance(node, ast.ParamRef):
            if node.number < 1 or node.number > self.allow.max_parameters:
                raise SqlRejected("SQL-015", "parameter reference out of range")
            self.max_param = max(self.max_param, node.number)
        elif isinstance(node, ast.A_Expr):
            self._a_expr(node, allow_aggregates=allow_aggregates)
        elif isinstance(node, ast.BoolExpr):
            if node.boolop not in (BoolExprType.AND_EXPR, BoolExprType.OR_EXPR, BoolExprType.NOT_EXPR):
                raise SqlRejected("SQL-014", "unsupported boolean expression")
            for arg in node.args:
                self._expr(arg, allow_aggregates=allow_aggregates)
        elif isinstance(node, ast.NullTest):
            self.n_predicates += 1
            self._expr(node.arg, allow_aggregates=allow_aggregates)
        elif isinstance(node, ast.FuncCall):
            self._func(node, allow_aggregates=allow_aggregates)
        elif isinstance(node, ast.CoalesceExpr):
            for arg in node.args:
                self._expr(arg, allow_aggregates=allow_aggregates)
        elif isinstance(node, ast.TypeCast):
            names = [n.lower() for n in _strings(node.typeName.names)]
            if (
                node.typeName.arrayBounds
                or node.typeName.setof
                or node.typeName.pct_type
                or names[-1] not in self.allow.cast_types
                or (len(names) == 2 and names[0] != "pg_catalog")
                or len(names) > 2
            ):
                raise SqlRejected("SQL-013", "cast target type is not on the allowlist")
            self._expr(node.arg, allow_aggregates=allow_aggregates)
        else:
            raise SqlRejected("SQL-014", "unsupported expression construct")

    def _a_expr(self, node: ast.A_Expr, *, allow_aggregates: bool) -> None:
        kind = node.kind
        allowed_kinds = {
            A_Expr_Kind.AEXPR_OP,
            A_Expr_Kind.AEXPR_IN,
            A_Expr_Kind.AEXPR_LIKE,
            A_Expr_Kind.AEXPR_ILIKE,
            A_Expr_Kind.AEXPR_BETWEEN,
            A_Expr_Kind.AEXPR_NOT_BETWEEN,
        }
        if kind not in allowed_kinds:
            raise SqlRejected("SQL-012", "operator form is not supported")
        name = _strings(node.name)
        if kind in (A_Expr_Kind.AEXPR_BETWEEN, A_Expr_Kind.AEXPR_NOT_BETWEEN):
            if len(name) != 1 or name[0].upper() not in ("BETWEEN", "NOT BETWEEN"):
                raise SqlRejected("SQL-012", "operator is not on the allowlist")
        elif len(name) != 1 or name[0] not in self.allow.operators:
            raise SqlRejected("SQL-012", "operator is not on the allowlist")
        if name[0] in ("=", "<>", "!=", "<", ">", "<=", ">=") or kind != A_Expr_Kind.AEXPR_OP:
            self.n_predicates += 1
        for side in (node.lexpr, node.rexpr):
            if side is None:
                continue
            if isinstance(side, (tuple, list)):
                if kind not in (A_Expr_Kind.AEXPR_IN, A_Expr_Kind.AEXPR_BETWEEN, A_Expr_Kind.AEXPR_NOT_BETWEEN):
                    raise SqlRejected("SQL-014", "unsupported list expression")
                for item in side:
                    self._expr(item, allow_aggregates=allow_aggregates)
            else:
                self._expr(side, allow_aggregates=allow_aggregates)

    def _func(self, node: ast.FuncCall, *, allow_aggregates: bool) -> None:
        names = [n.lower() for n in _strings(node.funcname)]
        if len(names) == 2 and names[0] == "pg_catalog":
            names = names[1:]
        if len(names) != 1:
            raise SqlRejected("SQL-011", "function is not on the allowlist")
        fname = names[0]
        if node.over is not None:
            raise SqlRejected("SQL-014", "window functions are not supported")
        if node.agg_filter is not None or node.agg_order or node.agg_within_group or node.func_variadic:
            raise SqlRejected("SQL-014", "unsupported function call modifiers")
        if fname in self.allow.aggregates:
            if not allow_aggregates:
                raise SqlRejected("SQL-014", "aggregates are not permitted in this clause")
            if node.agg_star and fname != "count":
                raise SqlRejected("SQL-014", "unsupported aggregate form")
            self.n_aggregates += 1
            for arg in node.args or ():
                self._expr(arg, allow_aggregates=False)
            return
        if fname not in self.allow.scalar_functions or node.agg_star or node.agg_distinct:
            raise SqlRejected("SQL-011", "function is not on the allowlist")
        self.n_functions += 1
        for arg in node.args or ():
            self._expr(arg, allow_aggregates=allow_aggregates)

    # -- helpers -------------------------------------------------------------

    def _resolve(self, ref: ast.ColumnRef) -> tuple[str, str]:
        fields = ref.fields
        if any(isinstance(f, ast.A_Star) for f in fields):
            raise SqlRejected("SQL-014", "'*' is only supported directly in the select list")
        parts = _strings(fields)
        if len(parts) == 1:
            matches = [t for t in self.scope.values() if parts[0] in self.allow.tables[t].columns]
            if len(set(matches)) != 1:
                raise SqlRejected("SQL-010", "column is not on the allowlist or is ambiguous")
            return matches[0], parts[0]
        if len(parts) == 2:
            table = self.scope.get(parts[0])
            if table is None or parts[1] not in self.allow.tables[table].columns:
                raise SqlRejected("SQL-010", "column is not on the allowlist")
            return table, parts[1]
        raise SqlRejected("SQL-014", "only column or alias.column references are supported")

    def _star(self, ref: ast.ColumnRef) -> None:
        self.select_star = True
        if len(ref.fields) == 1:
            self.projected += sum(len(self.allow.tables[t].columns) for t in self.scope.values())
            return
        if len(ref.fields) == 2 and isinstance(ref.fields[0], ast.String):
            table = self.scope.get(ref.fields[0].sval)
            if table is None:
                raise SqlRejected("SQL-010", "relation alias is not in scope")
            self.projected += len(self.allow.tables[table].columns)
            return
        raise SqlRejected("SQL-014", "unsupported '*' form")

    def _limit_value(self, node: Any) -> tuple[int | None, bool]:
        if node is None:
            return None, False
        if isinstance(node, ast.ParamRef):
            self._expr(node, allow_aggregates=False)  # counts the placeholder
            return None, True
        if isinstance(node, ast.A_Const) and isinstance(node.val, ast.Integer) and node.val.ival >= 0:
            return node.val.ival, False
        raise SqlRejected("SQL-014", "LIMIT/OFFSET must be a non-negative integer or parameter")

    @staticmethod
    def _conjuncts(node: Any) -> list[Any]:
        if isinstance(node, ast.BoolExpr) and node.boolop == BoolExprType.AND_EXPR:
            out: list[Any] = []
            for arg in node.args:
                out.extend(_Walker._conjuncts(arg))
            return out
        return [node]

    @staticmethod
    def _column_equality(node: Any) -> bool:
        return (
            isinstance(node, ast.A_Expr)
            and node.kind == A_Expr_Kind.AEXPR_OP
            and _strings(node.name) == ["="]
            and isinstance(node.lexpr, ast.ColumnRef)
            and isinstance(node.rexpr, ast.ColumnRef)
        )

    def _indexed_filter(self, node: Any) -> bool:
        if not isinstance(node, ast.A_Expr):
            return False
        if node.kind == A_Expr_Kind.AEXPR_OP and _strings(node.name)[0] not in ("=", "<", ">", "<=", ">="):
            return False
        if node.kind not in (A_Expr_Kind.AEXPR_OP, A_Expr_Kind.AEXPR_IN, A_Expr_Kind.AEXPR_BETWEEN):
            return False
        for col_side, val_side in ((node.lexpr, node.rexpr), (node.rexpr, node.lexpr)):
            if isinstance(col_side, ast.ColumnRef) and not isinstance(val_side, ast.ColumnRef):
                table, column = self._resolve(col_side)
                if column in self.allow.tables[table].indexed_columns:
                    return True
        return False


def normalize_parameters(parameters: Sequence[Any] | Mapping[str, Any] | None) -> list[Any]:
    """Positional parameter list for ``$1..$n``.

    The API sends a mapping; keys must be ``"1".."n"`` (or ``"$1".."$n"``) with
    no gaps. A sequence is taken as already positional.
    """

    if parameters is None:
        return []
    if isinstance(parameters, Mapping):
        positions: dict[int, Any] = {}
        for key, value in parameters.items():
            token = str(key).removeprefix("$")
            if not token.isdigit() or int(token) < 1:
                raise SqlRejected("SQL-015", "parameter keys must be positional ('1'..'n')")
            positions[int(token)] = value
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise SqlRejected("SQL-015", "parameter positions must be contiguous from 1")
        return [positions[i] for i in range(1, len(positions) + 1)]
    if isinstance(parameters, (list, tuple)):
        return list(parameters)
    raise SqlRejected("SQL-015", "unsupported parameter container")


def validate_sql(
    sql: str, parameters: Sequence[Any] | Mapping[str, Any] | None, allow: SqlAllowlist
) -> ValidatedQuery:
    """Parse and validate one candidate. Raises ``SqlRejected``; never executes anything."""

    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("SQL-001", "empty request")
    if len(sql.encode("utf-8")) > allow.max_sql_bytes:
        raise SqlRejected("SQL-001", "request exceeds the maximum accepted size")
    params = normalize_parameters(parameters)
    if len(params) > allow.max_parameters or not all(isinstance(p, ALLOWED_PARAMETER_TYPES) for p in params):
        raise SqlRejected("SQL-015", "unsupported parameter list")
    try:
        statements = parse_sql(sql)
    except ParseError:
        raise SqlRejected("SQL-002", "SQL could not be parsed") from None
    except Exception:  # defensive: parser internals must not leak input
        raise SqlRejected("SQL-002", "SQL could not be parsed") from None
    if len(statements) != 1:
        raise SqlRejected("SQL-003", "exactly one statement is required")
    stmt = statements[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise SqlRejected("SQL-004", "only SELECT statements are permitted")
    if stmt.valuesLists:
        raise SqlRejected("SQL-014", "VALUES lists are not supported")
    structure, limit_constant = _Walker(allow).select(stmt)
    if structure.n_params != len(params):
        raise SqlRejected("SQL-015", "parameter count does not match placeholders")
    return ValidatedQuery(sql=sql, structure=structure, limit_constant=limit_constant, parameters=params)
