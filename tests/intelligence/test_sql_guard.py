import pytest

from app.intelligence.sql_ast import SqlRejected, normalize_parameters, validate_sql


def rejected(sql, allow, params=None):
    with pytest.raises(SqlRejected) as exc:
        validate_sql(sql, params, allow)
    return exc.value.rule_id


@pytest.mark.parametrize(
    "sql, rule",
    [
        ("SELECT id FROM lab.products; DROP TABLE lab.products", "SQL-003"),
        ("SELECT id FROM lab.products; SELECT id FROM lab.products", "SQL-003"),
        ("INSERT INTO lab.products (id) VALUES (1)", "SQL-004"),
        ("UPDATE lab.products SET name = 'x'", "SQL-004"),
        ("DELETE FROM lab.products", "SQL-004"),
        ("DROP TABLE lab.products", "SQL-004"),
        ("TRUNCATE lab.products", "SQL-004"),
        ("COPY lab.products TO STDOUT", "SQL-004"),
        ("DO $$ BEGIN PERFORM 1; END $$", "SQL-004"),
        ("GRANT SELECT ON lab.products TO public", "SQL-004"),
        ("SET statement_timeout = 0", "SQL-004"),
        ("EXPLAIN ANALYZE SELECT id FROM lab.products", "SQL-004"),
        ("VALUES (1), (2)", "SQL-014"),
        ("WITH d AS (DELETE FROM lab.products RETURNING id) SELECT id FROM d", "SQL-005"),
        ("WITH x AS (SELECT id FROM lab.products) SELECT id FROM x", "SQL-005"),
        ("SELECT id INTO lab.copy FROM lab.products", "SQL-006"),
        ("SELECT id FROM lab.products FOR UPDATE", "SQL-007"),
        ("SELECT id FROM lab.products FOR SHARE", "SQL-007"),
        ("SELECT id FROM lab.products UNION SELECT id FROM lab.warehouses", "SQL-008"),
        ("SELECT usename FROM pg_catalog.pg_user", "SQL-009"),
        ("SELECT * FROM information_schema.tables", "SQL-009"),
        ("SELECT id FROM otherdb.lab.products", "SQL-009"),
        ("SELECT id FROM secrets", "SQL-009"),
        ("SELECT password FROM lab.products", "SQL-010"),
        ("SELECT id FROM lab.products p JOIN lab.warehouses w ON p.id = w.id", "SQL-010"),  # ambiguous id
        ("SELECT pg_sleep(10) FROM lab.products", "SQL-011"),
        ("SELECT pg_catalog.pg_sleep(10) FROM lab.products", "SQL-011"),
        ("SELECT pg_read_file('/etc/passwd') FROM lab.products", "SQL-011"),
        ("SELECT set_config('statement_timeout', '0', false) FROM lab.products", "SQL-011"),
        ("SELECT nextval('s') FROM lab.products", "SQL-011"),
        ("SELECT lab.evil(id) FROM lab.products", "SQL-011"),
        ("SELECT id FROM lab.products WHERE id @> $1", "SQL-012"),
        ("SELECT id FROM lab.products WHERE id = ANY($1)", "SQL-012"),
        ("SELECT id::regclass FROM lab.products", "SQL-013"),
        ("SELECT CAST(id AS lab.evil_type) FROM lab.products", "SQL-013"),
        ("SELECT id FROM lab.products WHERE id IN (SELECT product_id FROM lab.stock_levels)", "SQL-014"),
        ("SELECT id FROM lab.products WHERE EXISTS (SELECT 1 FROM lab.warehouses)", "SQL-014"),
        ("SELECT (SELECT max(id) FROM lab.warehouses) FROM lab.products", "SQL-014"),
        ("SELECT x FROM (SELECT id AS x FROM lab.products) s", "SQL-014"),
        ("SELECT * FROM generate_series(1, 1000000000)", "SQL-014"),
        ("SELECT id FROM lab.products TABLESAMPLE SYSTEM (50)", "SQL-014"),
        ("SELECT row_number() OVER () FROM lab.products", "SQL-014"),
        ("SELECT count(id) FILTER (WHERE id > 1) FROM lab.products", "SQL-014"),
        ("SELECT DISTINCT ON (category) id FROM lab.products", "SQL-014"),
        ("SELECT id FROM lab.products ORDER BY id FETCH FIRST 3 ROWS WITH TIES", "SQL-014"),
        ("SELECT 1", "SQL-014"),
        ("SELECT id FROM lab.products NATURAL JOIN lab.warehouses", "SQL-014"),
        ("SELECT p.id FROM lab.products p CROSS JOIN lab.warehouses w", "SQL-014"),
        ("SELECT p.id FROM lab.products p, lab.warehouses w", "SQL-014"),
        ("SELECT p.id FROM lab.products p JOIN lab.warehouses w ON true", "SQL-014"),
        ("SELECT p.id FROM lab.products p FULL JOIN lab.warehouses w ON p.id = w.id", "SQL-014"),
        ("SELECT id FROM lab.products WHERE id = $1", "SQL-015"),  # placeholder without a value
        ("SELEC id FROM lab.products", "SQL-002"),
        ("", "SQL-001"),
    ],
)
def test_rejects_outside_supported_subset(sql, rule, allow):
    assert rejected(sql, allow) == rule


def test_parameters_are_counted_not_substituted(allow):
    assert rejected("SELECT id FROM lab.products", allow, params=[1]) == "SQL-015"
    assert rejected("SELECT id FROM lab.products WHERE id = $1", allow, params=[{"x": 1}]) == "SQL-015"
    v = validate_sql("SELECT id FROM lab.products WHERE id = $1", ["1; DROP TABLE x"], allow)
    assert v.sql == "SELECT id FROM lab.products WHERE id = $1"  # value never merged into text


def test_oversized_request_rejected_before_parsing(allow):
    sql = "SELECT id FROM lab.products WHERE " + " OR ".join(["id = 1"] * 2000)
    assert rejected(sql, allow) == "SQL-001"


def test_parse_error_reason_does_not_echo_input(allow):
    with pytest.raises(SqlRejected) as exc:
        validate_sql("SELECT 'top-secret-literal' FRM lab.products", None, allow)
    assert "top-secret-literal" not in str(exc.value)
    assert "FRM" not in exc.value.reason


@pytest.mark.parametrize(
    "sql, params",
    [
        ("SELECT id, name FROM lab.products WHERE id = $1", [1]),
        ("SELECT p.id, s.quantity FROM lab.products p JOIN lab.stock_levels s ON s.product_id = p.id WHERE p.category = $1 LIMIT 50", ["x"]),
        ("SELECT p.id FROM lab.products p LEFT JOIN lab.stock_levels s ON s.product_id = p.id", []),
        ("SELECT id FROM lab.products WHERE id IN (1, 2, 3) AND price_cents BETWEEN 1 AND 99", []),
        ("SELECT id FROM lab.products WHERE name ILIKE $1 AND description IS NOT NULL AND NOT (id < 5 OR id > 9)", ["%x%"]),
        ("SELECT category, count(*) AS n FROM lab.products GROUP BY category HAVING count(*) > 2 ORDER BY n DESC", []),
        ("SELECT DISTINCT category FROM lab.products", []),
        ("SELECT lower(name), coalesce(price_cents, 0)::int8 FROM lab.products ORDER BY 1 LIMIT $1 OFFSET $2", [10, 20]),
        ("SELECT * FROM lab.products;", []),
        ("SELECT p.* FROM lab.products AS p", []),
    ],
)
def test_supported_subset_is_accepted(sql, params, allow):
    validate_sql(sql, params, allow)


def test_structure_flags(allow):
    s = validate_sql("SELECT count(*) FROM lab.stock_levels", [], allow).structure
    assert s.aggregate_only and not s.select_star and s.n_aggregates == 1

    s = validate_sql("SELECT * FROM lab.products", [], allow).structure
    assert s.select_star and not s.has_limit and not s.aggregate_only and s.n_projected_columns == 7

    v = validate_sql("SELECT id FROM lab.products WHERE sku = $1 ORDER BY id LIMIT 25", ["a"], allow)
    assert v.structure.filters_on_indexed and v.structure.order_by_indexed and v.limit_constant == 25

    s = validate_sql("SELECT id FROM lab.products WHERE sku = $1 OR name = $2", ["a", "b"], allow).structure
    assert not s.filters_on_indexed  # OR is not treated as a selective indexed filter

    s = validate_sql("SELECT category, count(*) FROM lab.products GROUP BY category", [], allow).structure
    assert not s.aggregate_only  # grouped aggregates produce many rows


def test_validated_query_repr_hides_sql(allow):
    v = validate_sql("SELECT id FROM lab.products WHERE name = 'secret-name'", [], allow)
    assert "secret-name" not in repr(v)


def test_api_parameter_mapping_is_positional(allow):
    assert normalize_parameters({"2": "b", "1": "a"}) == ["a", "b"]
    assert normalize_parameters({"$1": 5}) == [5]
    v = validate_sql("SELECT id FROM lab.products WHERE id = $1 AND sku = $2", {"1": 1, "2": "x"}, allow)
    assert v.parameters == [1, "x"]
    for bad in ({"name": 1}, {"1": 1, "3": 2}, {"0": 1}):
        with pytest.raises(SqlRejected) as exc:
            normalize_parameters(bad)
        assert exc.value.rule_id == "SQL-015"
