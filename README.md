# Curl Comparator Load Test

A lightweight Flask-based load testing dashboard that executes real `curl` commands against two target hosts and compares response behavior under concurrent traffic.

It is useful for migration validation, canary checks, regression detection, and performance baselining between API environments.

![Curl Comparator UI](image.png)

## What It Does

- Runs the same `curl` template against two hosts by replacing `{host}`.
- Supports per-request variable substitution (`{id}`, `{name}`, etc.).
- Simulates Locust-style concurrency with configurable users, spawn rate, and duration.
- Streams live results via Server-Sent Events (SSE).
- Compares responses as JSON (semantic compare) when possible, otherwise raw text.
- Captures `curl` timings using `time_total` and `time_starttransfer` (TTFB).
- Shows request-level diffs and aggregate metrics (avg, p50, p95, p99).

## Requirements

- Python `>=3.9`
- `curl` available in the runtime environment
- Pip for dependency installation

## Installation

Install as a package:

```bash
pip install .
```

Or install dependencies only:

```bash
pip install -r requirements.txt
```

## Running the Application

Development:

```bash
curl-compare-loadtest
```

Production example with Gunicorn:

```bash
gunicorn -w 4 -k gthread -b 0.0.0.0:8000 app:app
```

Open `http://localhost:8000`.

## Quick Start

1. Enter a `curl` template that includes `{host}`.
2. Provide `Host 1` and `Host 2` (domain only, no protocol).
3. Add variable lines (`key=value,key2=value2`), one line per request profile.
4. Configure users, spawn rate, and duration.
5. Run the test and inspect live status, latency metrics, and response diffs.

## Curl Template and Variables

### Required token

- `{host}` must exist in the template.

### Optional tokens

- Any `{token}` that matches keys in each variable line.
- `{vars}` is also supported and maps to the full raw variable line.

Example:

Template:

```bash
curl -X GET "https://{host}/v1/items?id={id}&name={name}"
```

Variable lines:

```text
id=123,name=alice
id=124,name=bob
```

Headers and body placeholders are also supported:

```bash
curl -X POST "https://{host}/v1/users" -H "X-Trace-Id: {trace_id}" -d '{"name":"{name}"}'
```

## Comparison Logic

- `MATCH`: responses are identical.
  - If both bodies are valid JSON, objects are compared structurally.
  - Otherwise, raw response text is compared.
- `DIFFERENCE`: responses differ.
- `EMPTY`: one or both responses are empty.
- `TEMPLATE ERROR`: template/render/command/timing issue occurred for at least one host.

## Runtime Limits and Input Guardrails

- `users`: `1` to `500`
- `spawn_rate`: `0.1` to `200` users/sec
- `duration_seconds`: `1` to `3600`
- per `curl` execution timeout: `30` seconds
- stored response body for diff display: up to `20000` chars per host

Out-of-range numeric values are clamped to safe limits.

## API Endpoints

- `GET /`  
  Serves the dashboard UI.
- `GET /stream`  
  Starts a run and streams events via SSE.
- `POST /stop?run_id=<id>`  
  Stops an active run.

`/stream` query parameters:

- `curl_template`
- `host1`
- `host2`
- `variables` (multiline `key=value,...`)
- `users`
- `spawn_rate`
- `duration_seconds`
- `run_id` (optional; generated if omitted)

## Packaging

Build distributable artifacts:

```bash
python -m build
```

Artifacts are created in `dist/` (wheel and source distribution).

## Project Metadata

- Package: `curl-comparator-loadtest`
- Entry point: `curl-compare-loadtest` -> `app:main`
- License: MIT

## Notes for Reliable Results

- Keep request templates deterministic where possible.
- Avoid including volatile fields (timestamps, random IDs) if strict matching is required.
- Use representative variable sets to simulate production-like traffic patterns.

## License

MIT License. See `LICENSE`.
