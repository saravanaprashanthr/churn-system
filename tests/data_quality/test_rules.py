"""Static checks on the rule catalog: every rule is well-formed before it ever touches a database."""

import re
from pathlib import Path

import pytest

from data_quality.rules import RULES, SQL_PARAMS, Dimension, Rule, Severity

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_TABLES = {"customers", "products", "orders", "order_items", "events"}
PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _all_sql(rule: Rule) -> str:
    return rule.checked_sql + "\n" + rule.violations_sql


def test_rule_ids_are_unique_and_well_formed():
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids))
    for rule_id in ids:
        assert re.fullmatch(r"[A-Z]{3}_[A-Z0-9_]+", rule_id), rule_id


def test_every_dimension_required_by_the_assignment_is_covered():
    covered = {r.dimension for r in RULES}
    assert covered == set(Dimension)


def test_both_severities_are_used():
    assert {r.severity for r in RULES} == {Severity.BLOCK, Severity.WARN}


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.rule_id)
def test_rule_fields_are_consistent(rule: Rule):
    assert rule.table in SOURCE_TABLES
    assert 0.0 <= rule.max_failure_rate < 1.0
    assert rule.key_columns, "key_columns identify violations in samples and quarantine"
    assert rule.description.strip()
    if rule.excludes_customers:
        assert rule.quarantine and rule.key_columns == ("customer_id",)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.rule_id)
def test_sql_uses_only_known_parameters(rule: Rule):
    sql = _all_sql(rule)
    assert set(PLACEHOLDER.findall(sql)) <= SQL_PARAMS
    # A bare % would be read by psycopg as a placeholder; literals must be written %%.
    stripped = PLACEHOLDER.sub("", sql).replace("%%", "")
    assert "%" not in stripped


@pytest.mark.parametrize("rule", [r for r in RULES if r.quarantine], ids=lambda r: r.rule_id)
def test_quarantine_rules_return_their_key_columns(rule: Rule):
    # Quarantined rows are built with jsonb_build_object over key_columns, so each key must be
    # a selected column: either SELECT * from the rule's table or named explicitly.
    select_clause = rule.violations_sql.split("FROM", 1)[0]
    for column in rule.key_columns:
        assert "*" in select_clause or column in select_clause, (rule.rule_id, column)


def test_packaged_ddl_matches_sql_script():
    packaged = (REPO_ROOT / "data_quality" / "ddl.sql").read_text()
    script = (REPO_ROOT / "sql" / "02_dq_tables.sql").read_text()
    assert packaged == script
