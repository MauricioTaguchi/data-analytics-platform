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
