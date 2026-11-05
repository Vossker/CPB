# API examples

All user calls include `X-API-Key`; worker calls include `X-Worker-Token`, and run mutations also
include `X-Lease-Token`. Replace the shell variables below with values returned by `cpb seed`.

## Upload, create, and submit

```bash
curl -H "X-API-Key: $PUBLISHER_KEY" \
  -F "file=@bundle.zip" \
  http://localhost:8000/api/v1/uploads/task-bundle

curl -H "X-API-Key: $PUBLISHER_KEY" -H 'Content-Type: application/json' \
  -d @create-task.json \
  http://localhost:8000/api/v1/tasks

curl -X POST -H "X-API-Key: $PUBLISHER_KEY" \
  "http://localhost:8000/api/v1/tasks/$TASK_ID/submit"
```

`create-task.json` contains the upload's key and SHA-256 plus the `specification` shown in
`examples/python-pytest-job/task.json`.

## Lease and run

```bash
curl -H "X-Worker-Token: $WORKER_TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://localhost:8000/api/v1/worker/lease

curl -X POST -H "X-Worker-Token: $WORKER_TOKEN" \
  -H "X-Lease-Token: $LEASE_TOKEN" \
  "http://localhost:8000/api/v1/runs/$RUN_ID/start"
```

The worker uploads each artifact as multipart form fields `run_id`, `kind=artifact`, `name`, and
`file`, then submits the returned key/hash/size metadata to `/runs/{run_id}/complete`. Use the
packaged `cpb-worker` instead of implementing this sequence manually.

## Inspect result and wallet

```bash
curl -H "X-API-Key: $PUBLISHER_KEY" \
  "http://localhost:8000/api/v1/tasks/$TASK_ID"
curl -H "X-API-Key: $PUBLISHER_KEY" \
  "http://localhost:8000/api/v1/tasks/$TASK_ID/artifacts"
curl -H "X-API-Key: $PUBLISHER_KEY" http://localhost:8000/api/v1/wallet
curl -H "X-API-Key: $PUBLISHER_KEY" \
  http://localhost:8000/api/v1/wallet/transactions
```
