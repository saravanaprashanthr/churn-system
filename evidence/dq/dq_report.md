# Data quality report `dq_20260701`

- **Gate status: PASS**
- as_of: 2026-07-01 (data strictly before this date) · volume lookback: 28 days
- generated: 2026-11-05T17:29:01+00:00 · duration: 22.7 s
- rules: 40 · passed: 34 · warnings: 6 · blocking: 0 · rows quarantined: 19,970

| Rule | Dimension | Table | Severity | Checked | Violations | Rate | Tolerance | Outcome | Quarantined |
|---|---|---|---|---:|---:|---:|---:|---|---:|
| `ORD_ORDER_DATE_NOT_NULL` | completeness | orders | block | 500,000 | 10,016 | 2.003% | 5.0% | **pass** | 10,016 |
| `ORD_REQUIRED_NOT_NULL` | completeness | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_REQUIRED_NOT_NULL` | completeness | order_items | block | 1,530,225 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_REQUIRED_NOT_NULL` | completeness | events | block | 10,000,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_PRODUCT_ID_NOT_NULL` | completeness | events | warn | 10,000,000 | 199,962 | 2.000% | 5.0% | **pass** | 0 |
| `CUS_MARKETING_OPT_IN_NOT_NULL` | completeness | customers | warn | 50,000 | 4,993 | 9.986% | 15.0% | **pass** | 0 |
| `PRD_CATEGORY_NOT_NULL` | completeness | products | warn | 2,000 | 97 | 4.850% | 0.0% | **warn** | 0 |
| `ORD_ORDER_ID_UNIQUE` | uniqueness | orders | block | 500,000 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `EVT_DUPLICATE_ROWS` | uniqueness | events | warn | 10,000,000 | 0 | 0.000% | 0.1% | **pass** | 0 |
| `EVT_EVENT_ID_UNIQUE` | uniqueness | events | warn | 10,000,000 | 29,883 | 0.299% | 0.0% | **warn** | 0 |
| `CUS_EMAIL_UNIQUE` | uniqueness | customers | warn | 50,000 | 516 | 1.032% | 0.0% | **warn** | 0 |
| `ORD_STATUS_VALID` | validity | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ORD_TOTAL_RANGE` | validity | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_QUANTITY_RANGE` | validity | order_items | block | 1,530,225 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_UNIT_PRICE_POSITIVE` | validity | order_items | block | 1,530,225 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `PRD_UNIT_PRICE_POSITIVE` | validity | products | block | 2,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_TYPE_VALID` | validity | events | block | 10,000,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `CUS_SIGNUP_NOT_FUTURE` | validity | customers | warn | 50,000 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `CUS_EMAIL_FORMAT` | validity | customers | warn | 50,000 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `CUS_COUNTRY_CODE` | validity | customers | warn | 50,000 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `CUS_ORDER_VOLUME_PLAUSIBLE` | validity | customers | warn | 945 | 23 | 2.434% | 5.0% | **pass** | 23 |
| `ORD_CUSTOMER_EXISTS` | referential_integrity | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_ORDER_EXISTS` | referential_integrity | order_items | block | 1,530,225 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_PRODUCT_EXISTS` | referential_integrity | order_items | block | 1,530,225 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_CUSTOMER_EXISTS` | referential_integrity | events | block | 10,000,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_PRODUCT_EXISTS` | referential_integrity | events | warn | 9,800,038 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ORD_TOTAL_MATCHES_ITEMS` | consistency | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ORD_HAS_ITEMS` | consistency | orders | block | 500,000 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `ITM_PRICE_MATCHES_PRODUCT` | consistency | order_items | warn | 1,530,225 | 0 | 0.000% | 5.0% | **pass** | 0 |
| `ORD_AFTER_SIGNUP` | consistency | orders | warn | 489,984 | 14,982 | 3.058% | 0.0% | **warn** | 0 |
| `EVT_AFTER_SIGNUP` | consistency | events | warn | 10,000,000 | 1,042,178 | 10.422% | 0.0% | **warn** | 0 |
| `EVT_PURCHASE_HAS_ORDERS` | consistency | events | warn | 700,840 | 683,885 | 97.581% | 0.0% | **warn** | 0 |
| `ORD_FRESHNESS` | timeliness | orders | block | 1 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `EVT_FRESHNESS` | timeliness | events | block | 1 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `ORD_NOT_FUTURE` | timeliness | orders | block | 489,984 | 0 | 0.000% | 1.0% | **pass** | 0 |
| `EVT_NOT_FUTURE` | timeliness | events | block | 10,000,000 | 9,931 | 0.099% | 1.0% | **pass** | 9,931 |
| `ORD_DAILY_VOLUME_DROP` | volume | orders | block | 28 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `ORD_DAILY_VOLUME_SPIKE` | volume | orders | warn | 28 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `EVT_DAILY_VOLUME_DROP` | volume | events | block | 28 | 0 | 0.000% | 0.0% | **pass** | 0 |
| `EVT_DAILY_VOLUME_SPIKE` | volume | events | warn | 28 | 0 | 0.000% | 0.0% | **pass** | 0 |

Outcome: **pass** = within tolerance; **warn** = logged, pipeline continues; **block** = pipeline stops.
