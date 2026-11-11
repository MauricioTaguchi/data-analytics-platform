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

## HTTP latency, errors, memory, and cost inputs

The standard-library [HTTP probe](../ops/http_probe.py) complements the analytical benchmark with a bounded GET workload against a running API. From the repository root, after starting the application:

```bash
python ops/http_probe.py \
  --url http://127.0.0.1:8000/health/ready \
  --requests 60 --concurrency 4 \
  --revision "$(git rev-parse HEAD)" \
  --output /tmp/dataflow-http-probe.json
```

For a locally running API process on Linux, add `--pid <api-process-pid>` to record RSS. The PID must exist in the probe's own PID namespace. A container's internal PID is not a host PID; use the host-visible container process PID, or run the probe in the same PID namespace. The full-stack CI job supplies its Uvicorn PID and uploads the JSON even when the probe fails. The analytical benchmark, real worker/scheduler recovery tests, and PostgreSQL/file restore drill remain separate CI jobs; this probe does not replace them.

| Evidence | Meaning and boundary |
| --- | --- |
| p50/p95/p99, minimum, maximum, samples | Nearest-rank request latency over all attempts. Successes include reading the response body; HTTP failures stop at headers. Connections are new for each request, and the workload uses fixed concurrency without a target arrival rate. |
| Status counts, error categories, error rate | Non-2xx responses, redirects, transport/timeouts, truncated bodies, and bodies over 2 MiB fail. No response contents or exception strings enter the report. |
| Completed requests/second and wall time | Observed throughput of this endpoint and workload, including failed attempts; not maximum sustainable application throughput. |
| Baseline/observed peak/growth RSS | One local Linux process sampled every 20 ms, including baseline and final samples. Short peaks can be missed; child processes, workers, database, cache, and total container memory are excluded. Without `--pid`, memory is explicitly unmeasured. An unreadable or disappearing requested process fails acceptance. |
| Revision input and SHA-256 | Operator-provided deployed revision plus a hash of the local backend Python sources and probe. A remote deployment must be independently checked against that revision; local files do not prove which release a remote endpoint runs. |
| Cost allocation | Optional operator-provided infrastructure cost/hour and currency, allocated by elapsed wall time. This is arithmetic from supplied inputs, not a measured bill or a verified price. No value is assumed by default. |

The default exit status is nonzero for any failed request. Choose a tolerance with `--max-error-rate` (a fraction from 0 to 1) only after defining the workload's acceptance criteria. `--max-p95-ms` optionally enforces an explicit latency budget; CI intentionally has no timing threshold inferred from synthetic samples. The JSON is written before an acceptance failure returns exit code 1.

For authenticated read endpoints, put a short-lived token in an environment variable and pass its **name** with `--token-env DATAFLOW_PROBE_TOKEN`. Do not put tokens in a URL or command-line argument. Redirects are blocked so credentials cannot be forwarded to another destination. Reports retain the target origin/path but omit query values, authorization headers, bodies, and error text; use a non-sensitive fixture path and protect the evidence accordingly. The maximum workload is 10,000 requests, 64 concurrent workers, and a 120-second socket timeout per request; start small and run larger workloads only on an environment you control.

If an operator has verified the allocated infrastructure price, supply it explicitly. For example, these shell variables must contain the operator's values; the command provides no price estimate:

```bash
python ops/http_probe.py --requests 300 --concurrency 6 \
  --hourly-cost "$DATAFLOW_ALLOCATED_HOURLY_COST" \
  --currency "$DATAFLOW_COST_CURRENCY" \
  --output /tmp/dataflow-http-cost-inputs.json
```

Allocated workload cost is `hourly_cost × wall_seconds / 3600`; cost per successful request divides that value by successful requests and is null when none succeed. Decide explicitly whether the input includes API, workers, database, cache, storage, monitoring and idle capacity. Variable egress, storage growth and provider minimum charges need separate accounting. A readiness-path allocation is not the cost of processing a dataset.

Before claiming a production budget or capacity, repeat representative upload → profile → transform → preview → report journeys at increasing concurrency with known fixture sizes, collect whole-service memory/CPU and storage metrics, retain failed-request details in secured logs, and record the actual deployment limits and provider charges. No production latency, cost, notification delivery, or recovery objective is established by the new local/CI checks.

## Checked HTTP journeys across file formats

[`app.operations.workload`](../backend/app/operations/workload.py) exercises the running API and actual asynchronous jobs. Its dedicated [Checked HTTP workload workflow](../.github/workflows/workload.yml) starts an isolated PostgreSQL database, Redis, one API process, a two-process Celery worker serving both queues, and the recovery scheduler. Every run uploads JSON evidence, records the effective non-secret deployment settings, and prints a bounded per-format summary with the source hash and hardware description. The workflow runs eight journeys at concurrency two, with 10,000 rows per input (80,000 uploaded rows across four formats). Passing it establishes correctness for that measured workload; it is not a throughput limit, production capacity guarantee, latency objective, or provider cost estimate.

Run against a **disposable environment you control**, with backend development dependencies installed:

```bash
cd backend
python -m app.operations.workload \
  --base-url http://127.0.0.1:8000 \
  --rows 128 --concurrency 2 --iterations 1 \
  --formats csv json xlsx parquet \
  --duration-limit 420 --job-timeout 90 \
  --revision "$(git rev-parse HEAD)" \
  --output /tmp/dataflow-workload.json
```

Each format contains the same deterministic four-column data: repeated rows, null numeric values, quoted commas, Unicode and embedded newlines. CSV/JSON/Parquet serialization is deterministic for the recorded dependencies; XLSX ZIP timestamps and workbook creation dates are normalized. Evidence includes both the file SHA-256 and a canonical logical-data SHA-256. There are no large committed input files.

Each journey registers a distinct synthetic account, logs out, logs in, creates a project, uploads a fixture, checks the imported dimensions and preview values, profiles it, previews and applies duplicate removal, repeats the transformation request to verify idempotency, generates a report, and downloads its PDF. It then logs out, proves that the refresh token was revoked, logs back in, reopens the project and dataset, verifies the export again, and undoes the transformation. Assertions check all exported rows, column order, values and nulls; the original byte digest after undo; profile duplicate counts; transformation preview rows; report revision and source digest; and project/dataset persistence. PDF validation checks its signature and recorded input provenance; visual layout is outside this harness.

| Recorded evidence | Measurement boundary |
| --- | --- |
| Per-request and per-operation p50/p95/p99, samples, statuses and unexpected failures | Successful requests include reading the bounded response body. Unexpected HTTP statuses finish at headers. HTTP connections are reused within each journey. Expected 401 responses test refresh revocation and count as successful checks, not unexpected errors. |
| Journey latency and successful journeys/second | Registration, all steps, worker queueing, polling, and cleanup are included. The pool has fixed bounded concurrency; it does not generate a controlled arrival rate or find a saturation point. |
| Job wait times | Start after the admission response and include polling delay. Admission request latency is recorded separately. A terminal failed job or wrong result fails the journey even when all HTTP responses were 200. |
| Fixture identity and exact correctness results | All formats must produce the expected logical table. Export parsing accepts equivalent numeric dtypes across file formats but compares exact values, row order, columns and nulls. |
| Driver hardware, source fingerprint and supplied deployment revision | CPU model, affinity, cgroup limits, OS, Python and dependency versions describe the process running the driver. `--server-on-driver-host` records an explicit assertion that API/workers share that host; the CI workflow supplies it. It does not measure whole-service utilization or database/Redis container allocation. A supplied remote revision is not independently attested. |
| Optional cost allocation | With both `--hourly-cost` and `--currency`, allocate the supplied infrastructure price over measured workload wall time. Cost defaults to `null`; setup, idle capacity, variable storage, network fees and a provider bill are not measured. No price is guessed. |

Acceptance requires every journey and its supported cleanup actions to succeed. The command writes the report before returning exit code 1 for workload failure. There is deliberately no timing threshold inferred from a small synthetic sample. Credentials, tokens, response bodies and exception messages are excluded from evidence; format, operation and fixed error categories identify failures.

Limits are explicit: 16–10,000 rows per file, at most eight concurrent journeys, 64 total journeys, 8 MiB per upload/response, 20,000 ordinary HTTP requests, 30 seconds per request, 180 seconds per job and 900 seconds of workload admission. The default ordinary request budget is 5,000. Deadline checks stop new work; already-running calls remain subject to their socket timeout. Cleanup has separate bounded requests so exhausting the normal budget does not prevent cancellation or logout. Cleanup can extend elapsed time beyond the admission deadline.

The harness cancels unfinished jobs, deletes generated reports and revokes its refresh sessions. **The current API has no project, dataset or account deletion endpoint.** Their rows, source files and history remain until the disposable deployment is destroyed; counts are reported explicitly. CI stops the created process groups, removes its upload/report directories, and GitHub destroys its job-scoped database/Redis containers. Against another deployment, its owner must reclaim those resources. Do not run this workload on a production account or mistake successful logout for complete data deletion.

To discuss capacity by hardware, retain the CI run URL, measured source hash, actual machine record, deployment settings, workload dimensions, concurrency, timings and failures together. Increase one workload dimension at a time on the same allocated hardware and repeat samples. No end-to-end reference numbers are published here until a real deployed run has completed; unit tests and the in-process API test establish correctness only.

### Resource use during the HTTP workload

The dedicated workflow runs [`sample_workload_resources.py`](../backend/scripts/sample_workload_resources.py) alongside the HTTP driver. It observes the API, Celery worker and scheduler process trees through Linux `/proc`, using PID plus process start time as identity. The evidence records current process counts, summed RSS, observed CPU deltas and cumulative CPU seconds per tree, plus peak observed values. CPU deltas from observed processes remain in the total after those processes exit; PID reuse cannot transfer a previous process's membership to an unrelated process. A newly observed child contributes its CPU since birth, while the first sample establishes the existing processes' baseline. Command lines and environment variables are never read.

Summed RSS is **not USS**: shared pages can be counted repeatedly between processes. CPU percentages use one fully occupied core as 100%, so multi-process totals can exceed 100%. Processes born and exited between samples, unobserved final CPU increments, and children never seen before reparenting are missed. These are sampled observations, not hard memory ceilings or complete CPU accounting.

PostgreSQL and Redis service containers are sampled separately with bounded `docker stats --no-stream` calls. Their Docker CPU percentages, used memory and memory limits are retained as separate metrics; Docker's memory accounting is not directly additive to process RSS. Sampling requests are one second apart after collection; Docker collection can extend the actual interval, which is visible in each sample's timestamp. The sampler has a 900-second ceiling, three-second Docker call timeout, signal handling and a stop file. It saves `resources.json` and prints `WORKLOAD_RESOURCES` before application teardown. CI fails if a selected process tree or container has no observations.

The resource window includes the short interval before and after HTTP execution; UTC timestamps allow alignment with `evidence.json`. Compare the recorded allocation, workload, sampled usage and correctness together. This measures the selected test processes and containers on the identified CI host; it does not establish production capacity, an OOM-safe limit, or maximum sustainable concurrency.
