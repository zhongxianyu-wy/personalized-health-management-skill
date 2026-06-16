# MinerU API Reference

This reference documents the exact MinerU 精准解析 API surface that the v3
input-conversion layer relies on. Open this file only when implementing or
debugging `scripts/mineru_client.py`. Everything not strictly required to
make a successful call lives in the official docs at
<https://mineru.net/apiManage/docs>.

## 1. Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `https://mineru.net/api/v4/extract/task` | POST | Submit a single public URL for parsing. |
| `https://mineru.net/api/v4/extract/task/batch` | POST | Submit up to 200 public URLs in one batch. |
| `https://mineru.net/api/v4/file-urls/batch` | POST | Request upload URLs for local files (the path v3 always uses). |
| `https://mineru.net/api/v4/extract-results/batch/{batch_id}` | GET | Poll a batch's results. |

Token registration: <https://mineru.net/apiManage/token>.

## 2. Token policy

- Bundled demo token (`config/formal.yaml::mineru.demo_token`) is for
  quick experience only. Mark `token_source=demo`, log only the last
  four characters, and warn the user that quota / validity may fail.
- A user-owned token belongs in a private local config (not in git).
  When provided via `--mineru-token` or the local config, set
  `token_source=user` and store the same last-four-only fingerprint.
- On 401 / 403 / quota / expiry errors, the client must surface
  "请更新 MinerU API token" together with the registration URL and stop
  the run. Never silently swap back to the demo token.

## 3. Local-file flow (default in v3)

1. `POST /api/v4/file-urls/batch` with one entry per local file:
   ```json
   {
     "enable_formula": false,
     "enable_table": true,
     "language": "ch",
     "model_version": "vlm",
     "is_ocr": true,
     "files": [
       {"name": "test.pdf", "data_id": "<sha1-or-uuid>", "is_ocr": true}
     ]
   }
   ```
2. The response returns one signed upload URL per file. PUT each local
   file into its upload URL with `Content-Type: application/octet-stream`.
3. After upload, the API automatically queues the batch — capture the
   `batch_id` from the original response.
4. Poll `GET /api/v4/extract-results/batch/{batch_id}` every
   `poll_interval_seconds` (default 5 s) until each file has
   `state=done` or `state=failed`, bounded by `poll_timeout_seconds`
   (default 1800 s).
5. For every successful file, the response provides URLs for
   `full_zip_url`, `content_list_url`, `layout_url`, etc. The client
   downloads and standardizes them on disk.

Network calls that fail transiently are retried per file according to
`config/formal.yaml::mineru.network_max_attempts`; the v3 default is 6
attempts per file. Auth, quota, and other non-retryable API errors still
surface immediately with the token or quota message.

## 4. URL-input flow

Use `extract/task` (single URL) or `extract/task/batch` (multiple URLs)
when the input is already a public URL. Polling logic is identical to
the local-file flow.

## 5. Limits (v3 defaults)

- `max_file_size_mb`: 200
- `max_pages_per_file`: 200
- `max_batch_files`: 200

If MinerU updates these limits, prefer the runtime config and let the
API's own error responses serve as the source of truth.

## 6. Standardized output (mandatory)

Every successful file produces a directory named after its `data_id`:

```
analysis_output/artifacts/mineru/<data_id>/
  raw_result.json
  content.md
  layout.json
  images/
```

For folder inputs, also write `analysis_output/artifacts/mineru/batch_result.json`
summarising the batch.

A top-level `analysis_output/artifacts/conversion_manifest.json` is the
single source of truth for downstream stages and must include:

```json
{
  "token_source": "demo|user",
  "token_fingerprint": "****abcd",
  "model_version": "vlm",
  "batch_id": "...",
  "files": [
    {
      "data_id": "...",
      "source_path": "...",
      "size_bytes": 1234567,
      "submitted_at": "...",
      "state": "done|failed|partial",
      "outputs": ["content.md", "layout.json", "raw_result.json", "images/"],
      "error": null
    }
  ]
}
```

## 7. Quality gate consumption

`scripts/validate_mineru_output.py` reads `content.md`, `layout.json`,
and the manifest. It does not re-call MinerU. Failure to pass the quality
gate halts the pipeline before any risk reasoning.

## 8. Model choice

- PDF / image / Office: `vlm` (default in `formal.yaml`).
- HTML inputs: `MinerU-HTML`.

The orchestrator chooses per-file based on extension. The client must
not hard-code `vlm` in code paths that handle HTML.

## 9. Failure semantics

| Failure | Behaviour |
|---|---|
| Single file in a batch fails | Mark that file `state=failed`, keep the batch. |
| All files fail | Manifest top-level `status=fail`, pipeline halts. |
| Auth (401/403) | Halt, write `auth_failed` reason, surface token rotation prompt. |
| Quota / 402 | Halt, write `quota_exhausted` reason. |
| Timeout while polling | Manifest top-level `status=timeout`, file-level state preserved. |

## 10. Local override hooks for tests

`scripts/mineru_client.py` must accept an injected HTTP transport
(callable returning a response-like object). This lets `tests/test_v3_mineru_contract.py`
exercise the client without network access while keeping the production
code free of mock branches.
