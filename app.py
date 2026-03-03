import itertools
import json
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context

app = Flask(__name__)

MAX_USERS = 500
MAX_SPAWN_RATE = 200
MAX_DURATION_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 30
TIMING_MARKER = "TIMING:"
TIMING_FORMAT = f"{TIMING_MARKER}%{{time_total}};%{{time_starttransfer}}"
MAX_DIFF_BODY_CHARS = 20000
ACTIVE_RUNNERS: Dict[str, "LocustStyleRunner"] = {}
RUNNERS_LOCK = threading.Lock()


@dataclass
class CurlExecutionResult:
    ok: bool
    body: str
    total_ms: float
    ttfb_ms: float
    error: str = ""


@dataclass
class LoadConfig:
    curl_template: str
    host1: str
    host2: str
    variable_sets: List[Tuple[str, Dict[str, str]]]
    users: int
    spawn_rate: float
    duration_seconds: int


class MetricsAggregator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0
        self.matched = 0
        self.difference = 0
        self.empty = 0
        self.errors = 0
        self.h1_total: List[float] = []
        self.h1_ttfb: List[float] = []
        self.h2_total: List[float] = []
        self.h2_ttfb: List[float] = []

    @staticmethod
    def _percentile(values: List[float], p: int) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = max(0, min(len(sorted_vals) - 1, int((p / 100.0) * len(sorted_vals) + 0.999999) - 1))
        return sorted_vals[idx]

    @staticmethod
    def _avg(values: List[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    def record(self, result: Dict) -> None:
        with self._lock:
            self.total += 1
            status = result["status"]
            if status == "MATCH":
                self.matched += 1
            elif status == "DIFFERENCE":
                self.difference += 1
            elif status == "EMPTY":
                self.empty += 1
            else:
                self.errors += 1

            if result["host1_total_ms"] > 0:
                self.h1_total.append(result["host1_total_ms"])
                self.h1_ttfb.append(result["host1_ttfb_ms"])
            if result["host2_total_ms"] > 0:
                self.h2_total.append(result["host2_total_ms"])
                self.h2_ttfb.append(result["host2_ttfb_ms"])

    def snapshot(self, started_at: float, spawned_users: int, active_users: int) -> Dict:
        with self._lock:
            elapsed = max(0.0001, time.monotonic() - started_at)
            match_pct = (self.matched / self.total * 100.0) if self.total else 0.0
            empty_pct = (self.empty / self.total * 100.0) if self.total else 0.0
            return {
                "total": self.total,
                "matched": self.matched,
                "difference": self.difference,
                "empty": self.empty,
                "errors": self.errors,
                "matched_pct": round(match_pct, 2),
                "empty_pct": round(empty_pct, 2),
                "elapsed_seconds": round(elapsed, 2),
                "rps": round(self.total / elapsed, 2),
                "spawned_users": spawned_users,
                "active_users": active_users,
                "host1": {
                    "avg_total": round(self._avg(self.h1_total), 2),
                    "p50_total": round(self._percentile(self.h1_total, 50), 2),
                    "p95_total": round(self._percentile(self.h1_total, 95), 2),
                    "p99_total": round(self._percentile(self.h1_total, 99), 2),
                    "avg_ttfb": round(self._avg(self.h1_ttfb), 2),
                    "p50_ttfb": round(self._percentile(self.h1_ttfb, 50), 2),
                    "p95_ttfb": round(self._percentile(self.h1_ttfb, 95), 2),
                    "p99_ttfb": round(self._percentile(self.h1_ttfb, 99), 2),
                },
                "host2": {
                    "avg_total": round(self._avg(self.h2_total), 2),
                    "p50_total": round(self._percentile(self.h2_total, 50), 2),
                    "p95_total": round(self._percentile(self.h2_total, 95), 2),
                    "p99_total": round(self._percentile(self.h2_total, 99), 2),
                    "avg_ttfb": round(self._avg(self.h2_ttfb), 2),
                    "p50_ttfb": round(self._percentile(self.h2_ttfb, 50), 2),
                    "p95_ttfb": round(self._percentile(self.h2_ttfb, 95), 2),
                    "p99_ttfb": round(self._percentile(self.h2_ttfb, 99), 2),
                },
            }


class LocustStyleRunner:
    def __init__(self, config: LoadConfig):
        self.config = config
        self.events: "queue.Queue[Dict]" = queue.Queue(maxsize=10000)
        self.metrics = MetricsAggregator()
        self._stop = threading.Event()
        self._counter = itertools.count(1)
        self._threads: List[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._spawned_users = 0
        self._active_users = 0
        self._started_at = time.monotonic()
        self._deadline = self._started_at + config.duration_seconds

    def _emit(self, payload: Dict) -> None:
        try:
            self.events.put_nowait(payload)
        except queue.Full:
            return

    def _mark_active(self, delta: int) -> None:
        with self._lifecycle_lock:
            self._active_users += delta

    def _spawned(self) -> None:
        with self._lifecycle_lock:
            self._spawned_users += 1

    def _lifecycle(self) -> Tuple[int, int]:
        with self._lifecycle_lock:
            return self._spawned_users, self._active_users

    def _worker(self, user_id: int) -> None:
        self._mark_active(1)
        var_len = len(self.config.variable_sets)
        try:
            while not self._stop.is_set() and time.monotonic() < self._deadline:
                index = next(self._counter)
                variables_line, variables_map = self.config.variable_sets[(index - 1) % var_len]
                result = process_request(
                    index=index,
                    curl_template=self.config.curl_template,
                    host1=self.config.host1,
                    host2=self.config.host2,
                    variables_line=variables_line,
                    variables_map=variables_map,
                    user_id=user_id,
                )
                self.metrics.record(result)
                self._emit({"type": "result", "payload": result})
        finally:
            self._mark_active(-1)

    def _run(self) -> None:
        spawn_interval = 1.0 / max(0.1, self.config.spawn_rate)

        for user_id in range(1, self.config.users + 1):
            if self._stop.is_set() or time.monotonic() >= self._deadline:
                break
            thread = threading.Thread(target=self._worker, args=(user_id,), daemon=True)
            thread.start()
            self._threads.append(thread)
            self._spawned()
            time.sleep(spawn_interval)

        while any(t.is_alive() for t in self._threads):
            spawned, active = self._lifecycle()
            self._emit(
                {
                    "type": "stats",
                    "payload": self.metrics.snapshot(self._started_at, spawned, active),
                }
            )
            time.sleep(1.0)

        for thread in self._threads:
            thread.join(timeout=0.1)

        spawned, active = self._lifecycle()
        self._emit(
            {
                "type": "stats",
                "payload": self.metrics.snapshot(self._started_at, spawned, active),
            }
        )
        self._emit({"type": "done"})

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def parse_variable_line(line: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for chunk in line.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Invalid variable pair '{item}', expected key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError("Variable key cannot be empty")
        parsed[key] = value
    if not parsed:
        raise ValueError("Variable line did not contain any key=value pair")
    return parsed


def render_template_tokens(template: str, values: Dict[str, str]) -> str:
    token_pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    used_keys = set(token_pattern.findall(template))
    missing = [key for key in used_keys if key not in values]
    if missing:
        missing.sort()
        raise ValueError(f"Missing template variables: {', '.join(missing)}")
    return token_pattern.sub(lambda m: values[m.group(1)], template)


def safe_build_command(
    curl_template: str,
    host: str,
    variables_line: str,
    variables_map: Dict[str, str],
) -> List[str]:
    if "{host}" not in curl_template:
        raise ValueError("Curl template must contain {host}")

    values = {"host": host, "vars": variables_line}
    values.update(variables_map)
    rendered = render_template_tokens(curl_template, values)

    cmd = shlex.split(rendered)
    if not cmd:
        raise ValueError("Empty curl command")
    if cmd[0] != "curl":
        raise ValueError("Command must start with curl")

    if "-s" not in cmd and "--silent" not in cmd:
        cmd.append("-s")

    for idx, part in enumerate(cmd):
        if part in ("-w", "--write-out"):
            if idx + 1 >= len(cmd):
                raise ValueError("Invalid existing curl -w/--write-out argument")
            cmd[idx + 1] = TIMING_FORMAT
            return cmd
        if part.startswith("--write-out="):
            cmd[idx] = f"--write-out={TIMING_FORMAT}"
            return cmd

    cmd.extend(["-w", TIMING_FORMAT])
    return cmd


def parse_timing_output(raw_stdout: str) -> Tuple[str, float, float]:
    marker_index = raw_stdout.find(TIMING_MARKER)
    if marker_index == -1:
        raise ValueError("Timing marker missing in curl output")

    body = raw_stdout[:marker_index]
    matches = re.findall(r"TIMING:([0-9]*\.?[0-9]+);([0-9]*\.?[0-9]+)", raw_stdout)

    if not matches:
      raise ValueError("No timing found")

    total_s, ttfb_s = matches[0]   # first occurrence

    total_ms = float(total_s) * 1000.0
    ttfb_ms = float(ttfb_s) * 1000.0
    return body, total_ms, ttfb_ms


def execute_curl(
    curl_template: str,
    host: str,
    variables_line: str,
    variables_map: Dict[str, str],
) -> CurlExecutionResult:
    try:
        cmd = safe_build_command(curl_template, host, variables_line, variables_map)
    except ValueError as exc:
        return CurlExecutionResult(False, "", 0.0, 0.0, str(exc))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CurlExecutionResult(False, "", 0.0, 0.0, "curl timed out after 30s")
    except Exception as exc:
        return CurlExecutionResult(False, "", 0.0, 0.0, f"execution error: {exc}")


    try:
        body, total_ms, ttfb_ms = parse_timing_output(result.stdout)
        return CurlExecutionResult(True, body, total_ms, ttfb_ms)
    except ValueError as exc:
        return CurlExecutionResult(False, result.stdout, 0.0, 0.0, str(exc))


def normalize_json(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return None


def compare_responses(body1: str, body2: str) -> str:
    if not body1.strip() and not body2.strip():
        return "EMPTY"
    if not body1.strip() or not body2.strip():
        return "EMPTY"
    parsed1 = normalize_json(body1)
    parsed2 = normalize_json(body2)
    if parsed1 is not None and parsed2 is not None:
        return "MATCH" if parsed1 == parsed2 else "DIFFERENCE"
    return "MATCH" if body1 == body2 else "DIFFERENCE"


def process_request(
    index: int,
    curl_template: str,
    host1: str,
    host2: str,
    variables_line: str,
    variables_map: Dict[str, str],
    user_id: int,
) -> Dict:
    res1 = execute_curl(curl_template, host1, variables_line, variables_map)
    res2 = execute_curl(curl_template, host2, variables_line, variables_map)

    if not res1.ok or not res2.ok:
        return {
            "index": index,
            "user_id": user_id,
            "variables": variables_line,
            "status": "TEMPLATE ERROR",
            "host1_total_ms": round(res1.total_ms, 2),
            "host1_ttfb_ms": round(res1.ttfb_ms, 2),
            "host2_total_ms": round(res2.total_ms, 2),
            "host2_ttfb_ms": round(res2.ttfb_ms, 2),
            "host1_error": res1.error,
            "host2_error": res2.error,
            "body1": res1.body[:MAX_DIFF_BODY_CHARS],
            "body2": res2.body[:MAX_DIFF_BODY_CHARS],
        }

    return {
        "index": index,
        "user_id": user_id,
        "variables": variables_line,
        "status": compare_responses(res1.body, res2.body),
        "host1_total_ms": round(res1.total_ms, 2),
        "host1_ttfb_ms": round(res1.ttfb_ms, 2),
        "host2_total_ms": round(res2.total_ms, 2),
        "host2_ttfb_ms": round(res2.ttfb_ms, 2),
        "host1_error": "",
        "host2_error": "",
        "body1": res1.body[:MAX_DIFF_BODY_CHARS],
        "body2": res2.body[:MAX_DIFF_BODY_CHARS],
    }


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Load Test Comparison Dashboard</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #fff;
      --ink: #0f172a;
      --sub: #475467;
      --ok: #ecfdf3;
      --warn: #fff6ed;
      --bad: #fef3f2;
      --line: #d0d5dd;
      --brand: #0f4c81;
    }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, #e8f0ff, transparent 40%),
        radial-gradient(circle at 90% 16%, #dff7f2, transparent 42%),
        var(--bg);
    }
    .wrap { max-width: 1240px; margin: 22px auto; padding: 0 16px 24px; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: 0 2px 8px rgba(16, 24, 40, 0.05);
    }
    h1 { margin: 0 0 12px; font-size: 23px; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .full { grid-column: 1 / -1; }
    label { font-size: 13px; color: var(--sub); display: block; margin-bottom: 5px; }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      font-size: 14px;
      box-sizing: border-box;
      font-family: inherit;
    }
    textarea { min-height: 120px; resize: vertical; }
    .btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    button {
      border: none;
      background: var(--brand);
      color: white;
      border-radius: 8px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 600;
    }
    button.secondary { background: #667085; }
    button.ghost { background: #98a2b3; }
    button.row-action {
      padding: 6px 10px;
      font-size: 12px;
      background: #344054;
    }
    .metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fafcff;
      font-size: 13px;
      line-height: 1.55;
    }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    th, td { border: 1px solid var(--line); padding: 7px; vertical-align: top; }
    th { background: #f8f9fc; text-align: left; }
    tr.match { background: var(--ok); }
    tr.diff { background: var(--bad); }
    tr.empty { background: var(--warn); }
    tr.error { background: #f2f4f7; }
    .modal {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(15, 23, 42, 0.65);
      z-index: 1000;
      padding: 16px;
    }
    .modal.open { display: flex; }
    .modal-card {
      background: #fff;
      border-radius: 12px;
      width: min(1100px, 100%);
      max-height: 88vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--line);
    }
    .modal-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #f8f9fc;
      font-weight: 700;
    }
    .modal-body {
      overflow: auto;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    .diff-added { color: #087443; }
    .diff-removed { color: #b42318; }
    .diff-changed { color: #175cd3; }
    @media (max-width: 980px) {
      .grid, .metrics { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>API DIFF with load test</h1>
      <div class="grid">
        <div class="full">
          <label for="curlTemplate">Curl Template (must contain {host}; supports {name}, {age}, ... anywhere)</label>
          <textarea id="curlTemplate">curl -X GET "https://{host}/api/items?{vars}"</textarea>
        </div>
        <div>
          <label for="host1">Host 1</label>
          <input id="host1" value="api1.example.com" />
        </div>
        <div>
          <label for="host2">Host 2</label>
          <input id="host2" value="api2.example.com" />
        </div>
        <div>
          <label for="users">Users (1-500)</label>
          <input id="users" type="number" min="1" max="500" value="20" />
        </div>
        <div>
          <label for="spawnRate">Spawn Rate users/sec (0.1-200)</label>
          <input id="spawnRate" type="number" min="0.1" max="200" step="0.1" value="10" />
        </div>
        <div>
          <label for="duration">Duration seconds (1-3600)</label>
          <input id="duration" type="number" min="1" max="3600" value="30" />
        </div>
        <div>
          <label for="maxRows">Max Rows On Screen</label>
          <input id="maxRows" type="number" min="100" max="5000" value="1000" />
        </div>
        <div class="full">
          <label for="vars">Variable lines (comma-separated key=value, e.g. name=alice,age=30)</label>
          <textarea id="vars">id=123,name=test
id=124,name=test2</textarea>
        </div>
      </div>
      <div class="btns">
        <button id="runBtn">Run Load Test</button>
        <button class="secondary" id="stopBtn">Stop</button>
        <button class="secondary" id="resetBtn">Reset Inputs</button>
        <button class="ghost" id="clearBtn">Clear Results</button>
      </div>
    </div>

    <div class="card metrics">
      <div class="box" id="runtimeBox"></div>
      <div class="box" id="summaryBox"></div>
      <div class="box" id="perfBox"></div>
    </div>

    <div class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>User</th>
            <th>Variables</th>
            <th>Status</th>
            <th>Host1</th>
            <th>Host2</th>
            <th>Error</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="resultBody"></tbody>
      </table>
    </div>
  </div>
  <div class="modal" id="diffModal">
    <div class="modal-card">
      <div class="modal-head">
        <span id="diffTitle">JSON Diff</span>
        <button class="ghost" id="closeDiffBtn">Close</button>
      </div>
      <div class="modal-body" id="diffContent"></div>
    </div>
  </div>

<script>
let source = null;
let maxRows = 1000;
let currentRunId = null;
const diffStore = {};

function ms(value) {
  return Number(value || 0).toFixed(2);
}

function rowClass(status) {
  if (status === "MATCH") return "match";
  if (status === "DIFFERENCE") return "diff";
  if (status === "EMPTY") return "empty";
  return "error";
}

function clearResults() {
  for (const key of Object.keys(diffStore)) {
    delete diffStore[key];
  }
  document.getElementById("resultBody").innerHTML = "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('\"', "&quot;");
}

function parseMaybeJson(raw) {
  try {
    return { ok: true, value: JSON.parse(raw || "") };
  } catch (_) {
    return { ok: false, value: null };
  }
}

function toPath(base, key) {
  if (base === "$") return `${base}.${key}`;
  return `${base}.${key}`;
}

function diffJson(left, right, path = "$", out = []) {
  const leftArr = Array.isArray(left);
  const rightArr = Array.isArray(right);
  const leftObj = left !== null && typeof left === "object";
  const rightObj = right !== null && typeof right === "object";

  if (leftArr && rightArr) {
    const max = Math.max(left.length, right.length);
    for (let i = 0; i < max; i += 1) {
      const p = `${path}[${i}]`;
      const hasL = i < left.length;
      const hasR = i < right.length;
      if (!hasL && hasR) out.push({ type: "added", path: p, value: right[i] });
      else if (hasL && !hasR) out.push({ type: "removed", path: p, value: left[i] });
      else diffJson(left[i], right[i], p, out);
    }
    return out;
  }

  if (leftObj && rightObj && !leftArr && !rightArr) {
    const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
    for (const key of [...keys].sort()) {
      const p = toPath(path, key);
      const hasL = Object.prototype.hasOwnProperty.call(left, key);
      const hasR = Object.prototype.hasOwnProperty.call(right, key);
      if (!hasL && hasR) out.push({ type: "added", path: p, value: right[key] });
      else if (hasL && !hasR) out.push({ type: "removed", path: p, value: left[key] });
      else diffJson(left[key], right[key], p, out);
    }
    return out;
  }

  if (JSON.stringify(left) !== JSON.stringify(right)) {
    out.push({ type: "changed", path, left, right });
  }
  return out;
}

function jsonValue(value) {
  return typeof value === "string" ? JSON.stringify(value) : JSON.stringify(value, null, 2);
}

function openDiffModal(rowId, rowInfo) {
  const entry = diffStore[rowId];
  if (!entry) return;

  const modal = document.getElementById("diffModal");
  const title = document.getElementById("diffTitle");
  const content = document.getElementById("diffContent");
  title.textContent = `JSON Diff - Request #${rowInfo.index}, User ${rowInfo.user_id}`;

  const leftParsed = parseMaybeJson(entry.body1);
  const rightParsed = parseMaybeJson(entry.body2);

  if (!leftParsed.ok || !rightParsed.ok) {
    content.innerHTML = `
      <strong>Non-JSON response detected.</strong>\n\n
      <span class="diff-removed">Host1:</span>\n${escapeHtml(entry.body1 || "(empty)")}\n\n
      <span class="diff-added">Host2:</span>\n${escapeHtml(entry.body2 || "(empty)")}
    `;
    modal.classList.add("open");
    return;
  }

  const diffs = diffJson(leftParsed.value, rightParsed.value);
  if (!diffs.length) {
    content.textContent = "No JSON differences.";
    modal.classList.add("open");
    return;
  }

  const lines = diffs.map((d) => {
    if (d.type === "added") {
      return `<span class="diff-added">+ ${escapeHtml(d.path)} = ${escapeHtml(jsonValue(d.value))}</span>`;
    }
    if (d.type === "removed") {
      return `<span class="diff-removed">- ${escapeHtml(d.path)} = ${escapeHtml(jsonValue(d.value))}</span>`;
    }
    return `<span class="diff-changed">~ ${escapeHtml(d.path)} | host1=${escapeHtml(jsonValue(d.left))} | host2=${escapeHtml(jsonValue(d.right))}</span>`;
  });
  content.innerHTML = lines.join("\\n");
  modal.classList.add("open");
}

function closeDiffModal() {
  document.getElementById("diffModal").classList.remove("open");
}

function addResultRow(r) {
  const body = document.getElementById("resultBody");
  const tr = document.createElement("tr");
  tr.className = rowClass(r.status);
  const err = [r.host1_error, r.host2_error].filter(Boolean).join(" | ");
  const rowId = `${r.index}-${r.user_id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  diffStore[rowId] = { body1: r.body1 || "", body2: r.body2 || "" };
  tr.innerHTML = `
    <td>${r.index}</td>
    <td>${r.user_id}</td>
    <td>${r.variables}</td>
    <td>${r.status}</td>
    <td>${ms(r.host1_total_ms)} ms (TTFB ${ms(r.host1_ttfb_ms)} ms)</td>
    <td>${ms(r.host2_total_ms)} ms (TTFB ${ms(r.host2_ttfb_ms)} ms)</td>
    <td>${err || "-"}</td>
    <td><button class="row-action diff-btn" data-row-id="${rowId}" data-index="${r.index}" data-user="${r.user_id}">Diff</button></td>
  `;
  body.prepend(tr);
  while (body.children.length > maxRows) {
    const last = body.lastChild;
    const btn = last.querySelector(".diff-btn");
    if (btn && btn.dataset.rowId) {
      delete diffStore[btn.dataset.rowId];
    }
    body.removeChild(last);
  }
}

function renderStats(s) {
  document.getElementById("runtimeBox").innerHTML = `
    <strong>Runtime</strong><br/>
    Elapsed: ${ms(s.elapsed_seconds)} s<br/>
    RPS (pairs): ${ms(s.rps)}<br/>
    Spawned Users: ${s.spawned_users}<br/>
    Active Users: ${s.active_users}
  `;

  document.getElementById("summaryBox").innerHTML = `
    <strong>Summary</strong><br/>
    Total: ${s.total}<br/>
    Matched: ${s.matched}<br/>
    Difference: ${s.difference}<br/>
    Empty: ${s.empty}<br/>
    Template Error: ${s.errors}<br/>
    % Matched: ${ms(s.matched_pct)}%<br/>
    % Empty: ${ms(s.empty_pct)}%
  `;

  document.getElementById("perfBox").innerHTML = `
    <strong>Host1</strong><br/>
    Avg: ${ms(s.host1.avg_total)} ms | P50: ${ms(s.host1.p50_total)} ms<br/>
    P95: ${ms(s.host1.p95_total)} ms | P99: ${ms(s.host1.p99_total)} ms<br/>
    Avg TTFB: ${ms(s.host1.avg_ttfb)} ms | P95 TTFB: ${ms(s.host1.p95_ttfb)} ms<br/><br/>
    <strong>Host2</strong><br/>
    Avg: ${ms(s.host2.avg_total)} ms | P50: ${ms(s.host2.p50_total)} ms<br/>
    P95: ${ms(s.host2.p95_total)} ms | P99: ${ms(s.host2.p99_total)} ms<br/>
    Avg TTFB: ${ms(s.host2.avg_ttfb)} ms | P95 TTFB: ${ms(s.host2.p95_ttfb)} ms
  `;
}

function stopStream() {
  if (source) {
    source.close();
    source = null;
  }
}

async function stopCurrentRun() {
  if (!currentRunId) return;
  try {
    await fetch(`/stop?run_id=${encodeURIComponent(currentRunId)}`, { method: "POST" });
  } catch (_) {
    // Ignore network stop errors on UI side.
  } finally {
    currentRunId = null;
  }
}

async function runStream() {
  await stopCurrentRun();
  stopStream();
  clearResults();
  maxRows = Math.max(100, Math.min(5000, parseInt(document.getElementById("maxRows").value || "1000", 10)));
  document.getElementById("maxRows").value = String(maxRows);
  currentRunId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  const params = new URLSearchParams({
    curl_template: document.getElementById("curlTemplate").value,
    host1: document.getElementById("host1").value,
    host2: document.getElementById("host2").value,
    users: document.getElementById("users").value,
    spawn_rate: document.getElementById("spawnRate").value,
    duration_seconds: document.getElementById("duration").value,
    variables: document.getElementById("vars").value,
    run_id: currentRunId
  });

  source = new EventSource(`/stream?${params.toString()}`);

  source.onmessage = (evt) => {
    if (evt.data === "DONE") {
      currentRunId = null;
      stopStream();
      return;
    }
    let message;
    try {
      message = JSON.parse(evt.data);
    } catch (err) {
      console.error("Invalid SSE JSON payload:", err, evt.data);
      return;
    }
    if (message.type === "result") {
      addResultRow(message.payload);
    } else if (message.type === "stats") {
      renderStats(message.payload);
    }
  };

  source.onerror = () => {
    currentRunId = null;
    stopStream();
  };
}

document.getElementById("runBtn").addEventListener("click", runStream);
document.getElementById("stopBtn").addEventListener("click", async () => {
  await stopCurrentRun();
  stopStream();
});
document.getElementById("clearBtn").addEventListener("click", clearResults);
document.getElementById("resetBtn").addEventListener("click", async () => {
  await stopCurrentRun();
  stopStream();
  document.getElementById("curlTemplate").value = 'curl -X GET "https://{host}/api/items?{vars}"';
  document.getElementById("host1").value = "api1.example.com";
  document.getElementById("host2").value = "api2.example.com";
  document.getElementById("users").value = "20";
  document.getElementById("spawnRate").value = "10";
  document.getElementById("duration").value = "30";
  document.getElementById("maxRows").value = "1000";
  document.getElementById("vars").value = "id=123,name=test\\nid=124,name=test2";
  clearResults();
});
document.getElementById("resultBody").addEventListener("click", (evt) => {
  const target = evt.target;
  if (!(target instanceof HTMLElement)) return;
  if (!target.classList.contains("diff-btn")) return;
  openDiffModal(target.dataset.rowId, {
    index: target.dataset.index,
    user_id: target.dataset.user,
  });
});
document.getElementById("closeDiffBtn").addEventListener("click", closeDiffModal);
document.getElementById("diffModal").addEventListener("click", (evt) => {
  if (evt.target.id === "diffModal") closeDiffModal();
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


def parse_load_config() -> Tuple[LoadConfig, str]:
    curl_template = request.args.get("curl_template", "").strip()
    host1 = request.args.get("host1", "").strip()
    host2 = request.args.get("host2", "").strip()
    variable_lines = [line.strip() for line in request.args.get("variables", "").splitlines() if line.strip()]
    if not curl_template or not host1 or not host2:
        return None, "Missing required input"
    if not variable_lines:
        return None, "No variable lines provided"
    variable_sets: List[Tuple[str, Dict[str, str]]] = []
    for idx, line in enumerate(variable_lines, start=1):
        try:
            variable_sets.append((line, parse_variable_line(line)))
        except ValueError as exc:
            return None, f"Invalid variables line {idx}: {exc}"

    try:
        users = int(request.args.get("users", "20"))
    except ValueError:
        users = 20
    users = max(1, min(users, MAX_USERS))

    try:
        spawn_rate = float(request.args.get("spawn_rate", "10"))
    except ValueError:
        spawn_rate = 10.0
    spawn_rate = max(0.1, min(spawn_rate, float(MAX_SPAWN_RATE)))

    try:
        duration_seconds = int(request.args.get("duration_seconds", "30"))
    except ValueError:
        duration_seconds = 30
    duration_seconds = max(1, min(duration_seconds, MAX_DURATION_SECONDS))

    return (
        LoadConfig(
            curl_template=curl_template,
            host1=host1,
            host2=host2,
            variable_sets=variable_sets,
            users=users,
            spawn_rate=spawn_rate,
            duration_seconds=duration_seconds,
        ),
        "",
    )


@app.route("/stream")
def stream():
    config, error = parse_load_config()
    run_id = request.args.get("run_id", "").strip() or str(uuid4())

    def generator():
        if error:
            payload = {
                "type": "result",
                "payload": {
                    "index": 0,
                    "user_id": 0,
                    "variables": "",
                    "status": "TEMPLATE ERROR",
                    "host1_total_ms": 0.0,
                    "host1_ttfb_ms": 0.0,
                    "host2_total_ms": 0.0,
                    "host2_ttfb_ms": 0.0,
                    "host1_error": error,
                    "host2_error": "",
                },
            }
            yield f"data: {json.dumps(payload)}\n\n"
            yield "data: DONE\n\n"
            return

        runner = LocustStyleRunner(config)
        with RUNNERS_LOCK:
            ACTIVE_RUNNERS[run_id] = runner
        runner.start()

        try:
            while True:
                try:
                    event = runner.events.get(timeout=1.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue

                if event["type"] == "done":
                    yield "data: DONE\n\n"
                    break

                yield f"data: {json.dumps(event)}\n\n"
        finally:
            runner.stop()
            with RUNNERS_LOCK:
                ACTIVE_RUNNERS.pop(run_id, None)

    return Response(stream_with_context(generator()), mimetype="text/event-stream")


@app.route("/stop", methods=["POST"])
def stop():
    run_id = request.args.get("run_id", "").strip()
    if not run_id:
        return jsonify({"ok": False, "error": "run_id is required"}), 400
    with RUNNERS_LOCK:
        runner = ACTIVE_RUNNERS.get(run_id)
    if not runner:
        return jsonify({"ok": False, "error": "run not found"}), 404
    runner.stop()
    return jsonify({"ok": True})


def main() -> None:
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
