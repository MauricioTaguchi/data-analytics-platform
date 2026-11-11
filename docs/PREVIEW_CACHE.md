# Bounded preview cache

New CSV, Excel and JSON imports retain the original file and record its original SHA-256 and size. During the existing supervised inspection, DataFlow derives the same first 100 rows that the browser requests. This presentation is cached only when its compact JSON body fits 256 KiB. No additional full-dataset parse, canonical dataset copy or derived filesystem artifact is needed.

## Why this representation

The current browser calls `page=1&page_size=100` and paginates that loaded sample locally. A complete Parquet copy would duplicate storage and add normalization decisions for mixed columns and nested JSON solely to serve this small sample. The bounded cache preserves existing presentation behavior—including late mixed-column inference, nulls, timestamps, Unicode, booleans, nested values and truncation—without changing analytical types. Original and transformed values remain the source of truth. Transformation outputs already use Parquet and its projected preview reader.

This is a presentation optimization, not a new canonical analytical format. A future workload that repeatedly scans whole original datasets may justify a separate, measured canonical representation and a migration strategy.

## Integrity and execution

- The cache is attached to the admitted snapshot, published in the same transaction as successful import, and carries a checksum plus source identity and a versioned presentation/parser contract.
- A hit stays inside the supervised child and verifies the original file before and after lookup. It imports neither Pandas/NumPy nor database packages. A missing or changed original fails integrity verification; cached rows do not mask corruption.
- A changed parser version, limits, presentation contract or cache checksum causes a miss. Requests extending beyond the cached prefix use the existing supervised parser. Increment `CACHE_VERSION` whenever presentation semantics change.
- Cache creation normalizes at most 100 rows and the configured preview columns. Bodies exceeding 256 KiB are discarded; admission continues without a cache. The existing child RSS, deadline and response-size budgets still apply.
- Cache misses do not populate data from unverified legacy files. Existing snapshots are not backfilled by this migration and continue using the bounded parser path. Future imports receive the optimization; no historical identity is invented.
- Undo selects the original snapshot and its existing cache. Retention clears cached rows when the source expires. The immutable source/audit manifest remains. Cache JSON is deferred in ORM reads so history listing does not load every retained sample.

The cache stores sampled dataset content in the database. Database backups and access controls must therefore protect that content as well as metadata. The per-snapshot 256 KiB bound does not replace database-capacity planning. SHA-256 checksums are not an administrator-proof authentication scheme; database/filesystem administrators remain trusted.

## Reproduce the local comparison

From `backend/`, using the configured environment:

```bash
python scripts/benchmark_preview_cache.py --rows 100000 --xlsx-rows 10000 --repeats 5 --output preview-cache-results.json
```

The script uses 12-column synthetic CSV/JSON fixtures with 100,000 rows and XLSX with 10,000 rows. Both modes start a fresh supervised child for each request, warm the filesystem, alternate execution order and compare the entire preview response for exact equality on every sample. Cached requests include both full source hash passes. The script records raw timings, effective limits, package/platform metadata and hashes of the measured working-tree files; it fails if those files change during measurement.

[Recorded local run](benchmarks/preview-cache-local-20261111.json) contains five repeats per mode. Its base Git commit identifies the checkout origin; source fingerprints identify the measured changes. The available environment reports nine logical CPUs and an AMD EPYC 9V74 processor. Cgroup quota files were unavailable, so no dedicated-core or host-memory entitlement is claimed. The child budget was 512 MiB, one execution thread and a 15-second deadline.

| Format | Source rows | Parser median | Cache median | Median ratio |
| --- | ---: | ---: | ---: | ---: |
| CSV | 100,000 | 0.719 s | 0.133 s | 5.39× |
| JSON | 100,000 | 1.009 s | 0.166 s | 6.10× |
| XLSX | 10,000 | 1.414 s | 0.117 s | 12.05× |

All measured responses were identical. Each cached document was approximately 19–20 KiB. These ratios include avoiding dataframe-package startup and parsing; they do not isolate a disk-format advantage.

These measurements cover warm local file reads and subprocess startup. They exclude HTTP, database retrieval, concurrent users, network storage, full application journeys and deployment costs. They are not a production capacity, RPO/RTO or service-level claim. See [Benchmarks](BENCHMARKS.md) for broader workload evidence.

## Validation

`tests/test_preview_cache.py` covers format/value parity, the 100-row and 256 KiB boundaries, later-page fallback, checksum/config invalidation, same-size source tampering, concurrent replacement, original-file loss, import publication, undo, retention and the parser-free child path. Existing preview-resource, snapshot-integrity, dataset-flow and execution-contract tests exercise the surrounding boundaries.
