# Reproducible performance evidence

The benchmark measures actual application query and supervised transformation code against deterministic synthetic Parquet fixtures. It checks exact output parity before reporting measurements. The committed [reference JSON](benchmarks/reference.json) contains the complete samples, machine and dependency metadata, source fingerprint, configuration, and timestamps.

These are single-machine synthetic measurements. They do not establish deployment throughput, maximum safe upload size, concurrency across replicas, production latency percentiles, or a service-level objective.

## Reproduce

Use Linux with `/proc`, Python 3.12, backend requirements, and the optional native-engine requirements. Run from `backend/`:

```bash
python -m app.operations.benchmark --output /tmp/dataflow-benchmark.json
```

The defaults measure 10,000 and 100,000 rows, five repeated warm queries per engine, three pairs of concurrent queries, at least 20 cache lookups, and one supervised transformation per engine and row count. For a longer local sample:

```bash
python -m app.operations.benchmark \
  --rows 10000 100000 \
  --repetitions 20 \
  --output /tmp/dataflow-benchmark.json
```

Fixtures and transformation outputs live in a temporary directory and are removed after measurement. No PostgreSQL, Redis, production data, credentials, or large tracked fixture is needed. Query/processing subprocesses use an isolated test configuration and one analytical thread per execution.

## Workload and measurement boundaries

Each fixture has 34 columns: a skewed region group, numeric revenue with missing and zero values, and 32 unrelated integer columns. The random seed is `20261013 + row_count`. About 80% of rows share the Central group before null injection. The benchmark calculates grouped revenue after a nonnegative-value filter, querying two columns. Both paths retain the same chart semantics and must return exactly equal JSON. Additional self-checks cover empty filtered results, all-null numeric groups, and zero-valued aggregates.

The baseline reads all 34 Parquet columns into Pandas and invokes `DashboardService._build_pandas_chart`. The optimized path invokes `query_parquet` and executes a projected DuckDB aggregation. Both use the production query admission gate. Unsupported semantic fallbacks are not part of this particular fixture; a fallback would fail the benchmark instead of silently changing what was measured.

A separate fresh subprocess runs each query engine. The first-query timing excludes Python imports and subprocess startup; the OS filesystem cache is **not flushed**, so it must not be interpreted as cold physical-disk performance. Warm timings rerun the query engine; they are not result-cache hits. Concurrent-pair timing measures completion of two queries submitted to two threads in the same process. Cache timing measures only the production cache's `get`/JSON decode, excluding authorization, dataset lookup, key calculation, HTTP serialization, and network latency.

Query RSS is sampled from Linux `/proc/self/status` every 2 ms. The report includes the imported application's baseline, highest observed resident memory, and growth during the timed workload. Sampling may miss brief peaks. This avoids attributing a parent's inherited allocation high-water mark to a fresh query process. Native transformation children use the operating system's `RUSAGE_CHILDREN.ru_maxrss`; supervisor high-water memory is reported separately and can include inherited allocations.

Processing measures the actual supervised `drop_columns` operation through `run_native` for Pandas and DuckDB. It includes child startup, resource supervision, input validation, transformation, Parquet output, and hashing. Both outputs must match the original projected frame exactly, including column order, types, nulls, values, and row order. This projection workload favors columnar execution and does not represent every transformation.

## Initial reference observation

The initial run used Linux x86-64, an AMD EPYC 9V74 host, an 8 GiB container memory ceiling and an eight-CPU-equivalent cgroup quota. The runner reported Python 3.12.14, Pandas 2.2.3, PyArrow 25.0.0, DuckDB 1.5.5, and NumPy 2.5.4. See the JSON for the precise runtime record and source hash. It was captured from a modified working tree; its `base_commit` identifies the starting revision, while `application_source_sha256` identifies the application sources actually measured.

| Rows | Query path | First query | Warm median | Two concurrent queries, median | Observed peak RSS |
| ---: | --- | ---: | ---: | ---: | ---: |
| 10,000 | Full-read Pandas | 26.827 ms | 11.896 ms | 29.041 ms | 243.375 MiB |
| 10,000 | Projected DuckDB | 20.423 ms | 13.848 ms | 16.549 ms | 222.648 MiB |
| 100,000 | Full-read Pandas | 117.151 ms | 99.928 ms | 166.660 ms | 505.953 MiB |
| 100,000 | Projected DuckDB | 23.169 ms | 25.896 ms | 22.770 ms | 223.523 MiB |

The small warm-query sample shows that an analytical engine is not automatically faster at every size: DuckDB's warm median was slightly slower for the 10,000-row fixture. At 100,000 rows, projecting two of 34 columns reduced observed memory and query time in this run. These observations depend on the fixture, hardware, scheduler, caches, and library versions.

| Rows | Supervised projection engine | Total duration | Execution-child peak RSS | Exact output parity |
| ---: | --- | ---: | ---: | --- |
| 10,000 | Pandas | 833.675 ms | 182.727 MiB | Passed |
| 10,000 | DuckDB | 332.203 ms | 133.320 MiB | Passed |
| 100,000 | Pandas | 1,126.034 ms | 242.266 MiB | Passed |
| 100,000 | DuckDB | 386.273 ms | 140.133 MiB | Passed |

## Using measurements in engineering decisions

Keep artifacts attached to the measured revision; compare like-for-like hardware, limits, dependency versions, and fixtures. Do not make CI fail on a single timing fluctuation. Treat an unexpected parity failure as a correctness regression and investigate sustained memory/latency changes over repeated runs. Before changing deployment limits, extend the workload with representative row widths, cardinalities, file formats, fallback paths, transformations, dataset sizes, and end-to-end concurrent users.

The [restore drill](DISASTER_RECOVERY.md#reproducible-restore-drill) records recovery duration separately. Query and transformation measurements do not establish backup RPO or recovery RTO.
