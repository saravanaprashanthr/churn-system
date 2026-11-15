"""Guided demo of the data quality gate in a throwaway schema.

Creates schema ``dq_sandbox`` with small, clean data, plants known problems one step at a time,
and runs the real gate against it. The real tables in ``public`` are never touched.

    DATABASE_URL=postgresql://... .venv/bin/python scripts/dq_sandbox_demo.py          # pauses between steps
    DATABASE_URL=postgresql://... .venv/bin/python scripts/dq_sandbox_demo.py --no-pause --keep
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data_quality.gate import GateConfig, Outcome, run_gate  # noqa: E402
from data_quality.report import write_artifacts  # noqa: E402
from data_quality.rules import RULES  # noqa: E402
from tests.seed import AS_OF, SEED_DAYS, SEED_SQL  # noqa: E402

SCHEMA = "dq_sandbox"
CONFIG = GateConfig(as_of=AS_OF, lookback_days=28)


def step(number: int, title: str, pause: bool) -> None:
    print(f"\n{'=' * 78}\nSTEP {number}: {title}\n{'=' * 78}")
    if pause and sys.stdin.isatty():
        input("(press Enter to run this step) ")


def show(conn: psycopg.Connection, query: str, params: tuple = ()) -> None:
    cursor = conn.execute(query, params)
    if cursor.description is None:
        raise RuntimeError(f"query returned no result set: {query}")
    columns = [c.name for c in cursor.description]
    rows = [[("" if v is None else str(v)) for v in row] for row in cursor.fetchall()]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(columns)]
    print("  " + " | ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  " + "-+-".join("-" * w for w in widths))
    for row in rows:
        print("  " + " | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    print(f"  ({len(rows)} rows)")


def gate_summary(conn: psycopg.Connection) -> None:
    gate = run_gate(conn, CONFIG)
    conn.commit()
    not_passing = [r for r in gate.results if r.outcome is not Outcome.PASS or r.violations]
    print(f"\n  Gate status: {'PASS' if gate.passed else 'FAIL (pipeline would stop, exit code 1)'}")
    print(f"  {len(gate.results)} rules ran; rules with any violation:")
    if not not_passing:
        print("    none: every rule found 0 bad rows")
    for r in not_passing:
        print(
            f"    {r.rule.rule_id:<28} severity={r.rule.severity.value:<5} bad={r.violations:<4} "
            f"rate={100 * r.failure_rate:6.2f}% tolerance={100 * r.rule.max_failure_rate:4.1f}% "
            f"-> {r.outcome.value.upper():<5} quarantined={r.quarantined}"
        )


def create_sandbox(conn: psycopg.Connection) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(f"SET search_path TO {SCHEMA}")
    ddl = (REPO_ROOT / "sql" / "schema.sql").read_text().splitlines()
    conn.execute("\n".join(line for line in ddl if not line.lstrip().upper().startswith("DROP")))
    for statement in filter(str.strip, SEED_SQL.split(";")):
        conn.execute(statement, {"as_of": AS_OF, "days": SEED_DAYS})
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-pause", action="store_true", help="run all steps without waiting for Enter")
    parser.add_argument("--keep", action="store_true", help="keep the dq_sandbox schema at the end")
    args = parser.parse_args()
    pause = not args.no_pause
    logging.basicConfig(level=logging.ERROR)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(url) as conn:
        step(1, f"Create a private sandbox schema '{SCHEMA}' with small, clean data", pause)
        create_sandbox(conn)
        print(f"  {SEED_DAYS} days before as_of={AS_OF}: 3 orders/day (1 item each), 5 view events/day.")
        show(
            conn,
            "SELECT 'customers' AS table_name, COUNT(*) AS rows FROM customers UNION ALL "
            "SELECT 'products', COUNT(*) FROM products UNION ALL SELECT 'orders', COUNT(*) FROM orders "
            "UNION ALL SELECT 'order_items', COUNT(*) FROM order_items UNION ALL "
            "SELECT 'events', COUNT(*) FROM events",
        )
        print("\n  A few orders:")
        show(conn, "SELECT * FROM orders ORDER BY order_id LIMIT 3")

        step(2, "Run the gate on the clean data: all 40 rules should find nothing", pause)
        gate_summary(conn)

        step(3, "Plant three problems", pause)
        conn.execute("INSERT INTO orders VALUES (9001, 1, NULL, 'completed', 20.00)")
        conn.execute("INSERT INTO order_items VALUES (9001, 9001, 1, 2, 10.00)")
        conn.execute("INSERT INTO events VALUES (99001, 2, '2027-02-14 09:00', 'view', 1, 'future')")
        conn.execute("UPDATE orders SET order_total = 25.00 WHERE order_id = 10")
        conn.commit()
        print("  a) order 9001 has no order_date")
        print("  b) event 99001 is dated 2027-02-14, after the run date")
        print("  c) order 10 says order_total = 25.00, but its item is 2 x 10.00 = 20.00")
        show(
            conn,
            "SELECT o.order_id, o.order_date, o.order_total, SUM(i.quantity * i.unit_price) AS items_total "
            "FROM orders o JOIN order_items i USING (order_id) WHERE o.order_id IN (10, 9001) "
            "GROUP BY 1, 2, 3 ORDER BY 1",
        )

        step(4, "Look inside one rule: it is just a SQL query that returns the bad rows", pause)
        rule = next(r for r in RULES if r.rule_id == "ORD_TOTAL_MATCHES_ITEMS")
        print(
            f"  {rule.rule_id}  (dimension={rule.dimension.value}, severity={rule.severity.value}, "
            f"tolerance={100 * rule.max_failure_rate:.0f}%, quarantine={rule.quarantine})"
        )
        print("  " + "\n  ".join(line.strip() for line in rule.violations_sql.strip().splitlines()))
        print("\n  Running that query by hand:")
        show(conn, f"SELECT order_id, order_total, items_total FROM ({rule.violations_sql}) v")  # noqa: S608

        step(5, "Run the gate again: it catches all three and quarantines the rows", pause)
        gate_summary(conn)
        print("\n  The quarantine table (rows copied aside, never deleted from orders/events):")
        show(
            conn,
            "SELECT rule_id, table_name, row_key FROM dq_quarantine WHERE run_id = %s ORDER BY rule_id",
            (CONFIG.run_id,),
        )
        print("\n  The original rows are still in the source tables:")
        show(conn, "SELECT order_id, order_date FROM orders WHERE order_id = 9001")

        step(6, "Run the same date again: results are replaced, not duplicated (idempotent)", pause)
        gate_summary(conn)
        show(
            conn, "SELECT COUNT(*) AS quarantine_rows FROM dq_quarantine WHERE run_id = %s", (CONFIG.run_id,)
        )

        step(7, "Plant a serious problem: a whole day of orders is missing", pause)
        conn.execute(
            "DELETE FROM order_items WHERE order_id IN "
            "(SELECT order_id FROM orders WHERE order_date::date = %s::date - 3)",
            (AS_OF,),
        )
        conn.execute("DELETE FROM orders WHERE order_date::date = %s::date - 3", (AS_OF,))
        conn.commit()
        print(
            f"  Deleted all orders of {AS_OF - timedelta(days=3)}. "
            "A missing day usually means a broken extract."
        )
        gate_summary(conn)

        step(8, "The report written for this run", pause)
        gate = run_gate(conn, CONFIG)
        conn.commit()
        run_dir = write_artifacts(gate, REPO_ROOT / "artifacts" / "dq_sandbox", conn)
        print(
            f"  Files in {run_dir.relative_to(REPO_ROOT)}: "
            f"{', '.join(sorted(p.name for p in run_dir.iterdir()))}"
        )
        markdown = (run_dir / "dq_report.md").read_text().splitlines()
        print("  " + "\n  ".join(markdown[:8]))

        if args.keep:
            print(f"\nSchema '{SCHEMA}' kept. Explore it with: SET search_path TO {SCHEMA};")
        else:
            conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
            conn.commit()
            print(f"\nSandbox schema '{SCHEMA}' dropped. The real data was never touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
