# API examples

## Register and create a project

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name":"Data Analyst","email":"analyst@example.com","password":"password12345"}'

curl -X POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Revenue analytics","description":"Commercial performance indicators"}'
```

## Upload and monitor import

```bash
curl -X POST http://localhost:8000/api/v1/datasets/project/1 \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -F "file=@sales.csv"

curl http://localhost:8000/api/v1/datasets/jobs/TASK_ID \
  -H "Authorization: Bearer ACCESS_TOKEN"
```

## Preview and enqueue a transformation

```bash
curl -X POST http://localhost:8000/api/v1/datasets/1/transform/preview \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"operation":"drop_duplicates","parameters":{},"expected_version":1}'

curl -X POST http://localhost:8000/api/v1/datasets/1/transform \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Idempotency-Key: cleanup-sales-v1" \
  -H "Content-Type: application/json" \
  -d '{"operation":"drop_duplicates","parameters":{},"expected_version":1}'
```

Use the returned `task_id` with the job endpoint before requesting the refreshed dataset preview.
