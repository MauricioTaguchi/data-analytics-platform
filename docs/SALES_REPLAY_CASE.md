# Which sales channel led after a duplicate was removed?

A sales manager wants to highlight the channel with the highest recorded order value. Retail appears to lead in this five-row batch. One row, however, was imported twice.

The [example CSV](sample-data/sales-replay.csv) is a **synthetic extract**. It starts with orders `100002` through `100004` from [sales.csv](sample-data/sales.csv), repeats `100002`, and adds a second monitor purchase (`100005`). The notebook order `100001` is outside this batch. Amounts in this exercise are in BRL.

## Decide which records to keep

| Orders | Evidence in the batch | Decision |
| --- | --- | --- |
| `100002` twice | Same order ID and identical values in every column. This is the known replay in this extract. | Count the BRL 899.90 chair sale once. |
| `100003` and `100005` | Same customer, date, product and BRL 699.00 total. The example records two distinct purchases with separate order IDs. | Count both monitor sales. |
| `100004` | The keyboard order has no recorded amount. | Keep the order, exclude it from the sum and report one missing amount. |

Here, one source record represents one order. For a source with multiple line items per order, check its line identifier first. If the same order ID arrives with conflicting totals, reconcile the records with the source before choosing either amount.

## What changes in the report?

| Recorded order value (BRL) | Before cleanup | After removing the replay |
| --- | ---: | ---: |
| Retail | 1,799.80 | 899.90 |
| Online | 1,398.00 | 1,398.00 |
| All known amounts | 3,197.80 | 2,297.90 |

Highlight **Online**, which leads by BRL 498.10 on known amounts in this batch. Flag the missing Online amount for follow-up. Matching on customer, date and total alone would also discard the second monitor purchase, cut Online to BRL 699.00, and incorrectly put Retail back in first place.

## Reproduce in DataFlow

1. [Run the complete platform](../README.md#run-the-complete-platform) and upload the [example CSV](sample-data/sales-replay.csv) in **Datasets**.
2. In **Dashboards**, group by `channel`, choose `total` as the measure and `sum` as the aggregation. Compare the chart with the **Before cleanup** column.
3. In **Transformations**, choose **Remove duplicates**, preview the impact, then apply. The operation compares all columns. Five rows become four.
4. Rebuild the chart on the updated dataset to confirm Online leads. Undo the transformation and rebuild to recover the original totals.

## Execute the correctness replay

With the backend dependencies and development environment configured, run from `backend/`:

```sh
python -m app.operations.sales_case
python -m pytest tests/test_sales_case.py tests/test_chart_integrity.py -q
```

The command uses the actual `drop_duplicates` operation and both chart engines, checks the documented totals, keeps both distinct monitor purchases and leaves the source CSV unchanged. It prints the source SHA-256, row counts, missing-value counts and before/after metrics. It performs no account creation or external submission.

After cleanup, the mean recorded order is **BRL 765.97**, using three known amounts (`2,297.90 / 3`). The missing fourth amount remains unresolved. Averaging the two channel means would give BRL 799.45 and weight unequal groups incorrectly. See [metric definitions](CHART_METRICS.md).

## Five-minute review

| Time | Recruiter or product reviewer | Engineering reviewer |
| --- | --- | --- |
| 0–1 min | State the decision: which channel leads on known amounts? | Inspect the CSV grain and the replay versus repeat-purchase distinction. |
| 1–3 min | Compare the before/after table and the missing amount. | Run the replay; compare both engines, the operation and its tests. |
| 3–4 min | Explain why Online leads after cleanup. | Check the three-value mean denominator and rejection of non-additive pies. |
| 4–5 min | Ask whether the result would support the intended decision. | Inspect limits: synthetic data, sampled demo, single-node processing and separate recovery tests. |

## Proposed pilot, not completed validation

No real users, external professional approval or customer feedback are claimed. For a future consented pilot, ask one analyst to reproduce the decision without coaching, then explain which row was removed, why the two monitor orders remain, and why one amount is still unknown. Ask one engineer to run the command and challenge the metric definitions and failure boundaries.

Record each participant's actual completion, errors, questions and time to the correct decision; retain only agreed, non-sensitive notes. Success for this narrow pilot means both can reproduce the totals, identify the missing amount and explain the mean denominator. A failure becomes a recorded issue and another test or usability change. Leave these outcomes unfilled until the sessions occur; this exercise alone does not demonstrate production adoption, savings or seniority.
