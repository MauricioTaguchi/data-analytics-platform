# Dataset quality contracts

Quality contracts describe explicit expectations for a dataset. Saving a contract
creates an immutable version. Evaluations bind that version to the dataset
revision and snapshot admitted by the request; a later transformation does not
change the data being checked. These checks are separate from the preview's
heuristic quality score.

## Rule semantics

| Kind | Configuration | What fails |
| --- | --- | --- |
| `not_null` | `column` | Null or missing cells; empty text is present. |
| `unique` | `column` | Repeated non-null values after the first occurrence; nested arrays, structs, and binary values are compared in full. |
| `range` | `column`, `min` and/or `max` | Non-null values outside inclusive bounds, non-finite numbers, booleans, or non-numeric values. Nulls require a separate `not_null` rule. |
| `accepted_values` | `column`, scalar `values` | Values absent from the allowed set. Booleans differ from numbers; null must be explicitly allowed. |
| `schema` | `expected_schema`, optional `allow_extra_columns` | Missing columns or different Pandas dtype names; extra columns fail unless explicitly allowed. |

A missing column fails a column rule even when no rows can be checked. An empty
dataset passes value rules vacuously; schema checks still apply. Uniqueness and
range exclude nulls from `checked_count`. Schema counts compare columns, not rows.
Rules expose counts and generic explanations; evaluation evidence contains no
raw row samples. Contracts themselves contain the column names and allowed
values configured by the owner.

## Save and evaluate

Both requests require the authenticated dataset owner's bearer token. Integer
identifiers and revision numbers below are examples.

```http
POST /api/v1/datasets/42/quality/contracts
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Orders quality",
  "expected_contract_version": 0,
  "rules": [
    {"id": "required_id", "kind": "not_null", "column": "order_id"},
    {"id": "unique_id", "kind": "unique", "column": "order_id"},
    {"id": "positive_total", "kind": "range", "column": "total", "min": 0}
  ]
}
```

The `201` response includes the new contract `id` and `version`. Use the latest
version as `expected_contract_version` when saving a replacement; a stale version
returns `409` rather than overwriting another edit. List versions through
`GET /api/v1/datasets/42/quality/contracts`.

```http
POST /api/v1/datasets/42/quality/evaluations
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: orders-quality-revision-3

{"contract_id": 7, "expected_version": 3}
```

The `202` response contains `evaluation_id`, `task_id`, `status`, and `reused`.
Admission stores the evaluation, durable job, and outbox event in one transaction.
The worker evaluates the complete admitted snapshot in a supervised subprocess
with memory, wall-time, and dataset-shape limits. This is bounded in-memory
evaluation; it is not distributed or streaming execution.

Poll `GET /api/v1/datasets/jobs/{task_id}` for execution progress. Retrieve durable
evidence at `GET /api/v1/datasets/42/quality/evaluations/{evaluation_id}` or list
recent evaluations at `GET /api/v1/datasets/42/quality/evaluations`. The evidence
includes the contract version, dataset revision, snapshot SHA-256, evaluator
version, counts, and per-rule outcomes. `stale: true` means the dataset now has a
different revision; the historical result still describes its recorded snapshot.

## Outcomes and retention

* `completed` with `passed: true`: all configured rules passed.
* `completed` with `passed: false`: evaluation succeeded, but data violated one or
  more rules. The execution job is successful.
* `error`: execution could not finish, including resource limits or snapshot
  integrity failures. `passed` and `result` remain null.
* `cancelled`: the job was cancelled before publishing evidence. No quality pass
  or fail is asserted.

Terminal evaluation states are persisted atomically with job failure or
cancellation, including scheduler recovery without a worker callback. Contract
versions and evidence remain after job history retention. An idempotent replay
reuses the original evaluation, including terminal outcomes; its old job may
already be unavailable, so retrieve the evaluation directly. Use a new key to
request a new execution. Snapshot byte retention is separate: retained evidence
does not promise that old source files remain available forever.

Contracts allow at most 50 rules, 1,000 accepted values per rule, 1,000 expected
schema columns, and 30 KB of serialized configuration. Results contain bounded
counts rather than failing cell samples. Unsupported value types fail execution
rather than substituting truncated preview values during comparison.

## Verification

`backend/tests/test_quality_contracts.py` covers rule semantics, exact nested value
comparison, tenant ownership, version conflicts, snapshot binding, integrity
errors, and evidence surviving job deletion. The real-process scenario in
`backend/integration/test_quality_evaluation.py` covers broker delivery, duplicate
delivery, and evidence after API restart. Follow [CONTRIBUTING](../CONTRIBUTING.md)
for the required integration environment before running that suite.
