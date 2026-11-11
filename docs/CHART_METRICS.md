# What each chart measures

Charts operate on the current dataset revision after their saved filters. The local demo uses only its loaded preview, up to 100 rows. It does not establish totals for an entire uploaded file.

| Selection | Calculation and unit | Missing values |
| --- | --- | --- |
| Sum | Sum of recorded numeric values; same unit as the measure. | Missing/non-finite numeric observations are excluded. An empty sum is zero, with zero contributing rows disclosed. |
| Mean | Arithmetic mean of usable source rows, separately per category or across all rows for a KPI. Same unit as the measure. | Missing/non-finite observations are excluded from numerator and denominator. No observations gives an unavailable value. |
| Min / max | Smallest/largest usable numeric value; same unit as the measure. | No usable observations gives an unavailable value. |
| Count | Non-null cells in the selected measure, not distinct orders or users. Duplicates and empty strings count. A pie without a measure counts source rows. | Null cells are excluded. |
| Scatter | Original finite numeric X/Y pairs; each point is one row, retaining repeated X values. | A row without both coordinates is omitted as a pair. |
| Histogram | Number of numeric observations per interval; unit is rows per interval. | Missing values are excluded; the live API rejects non-finite histogram inputs. |

The response and workspace show the metric, selected columns, filtered source-row count, contributing-value count and omitted-value count. A source unit comes from the dataset; a column named `total` alone does not establish a currency. A count can legitimately include multiple rows for the same business entity.

## Why a pie accepts only sum and count

Pie slices represent parts of an additive total. The grouped **Other** slice combines additive contributions, preserving that total. Means, minima and maxima cannot be added to form that total. Compare them with bars, lines or a KPI instead; the API rejects a non-additive pie, including an older saved definition when recalculated.

For example, four values of 10 in category A and one value of 100 in B give category means of 10 and 100. The overall mean is **28**, using five observations. Neither their sum (110) nor their unweighted average (55) is the overall mean. A missing sixth value does not enter that denominator.

## Reading incomplete or empty results

- The UI shows up to 12 bar categories, 24 line categories, 100 scatter pairs or 20 table rows; server limits and total counts remain visible. Unshown categories are not implicitly zero.
- Pie results retain all category contributions, grouping the remainder as Other. Negative or non-finite category values cannot be displayed as shares.
- An empty filtered population is identified separately from a recorded zero. Missing line values are gaps; missing bars have no bar. An all-missing sum/count displays its zero-observation basis.
- Source-row and contributing-value counts describe the filtered population before display limits. They are not a confidence interval, a data-quality guarantee or proof of completeness in the originating business system.

Use the [sales replay case](SALES_REPLAY_CASE.md) to check a concrete decision and its denominators.
