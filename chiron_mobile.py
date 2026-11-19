#!/usr/bin/env python3
"""
Chiron LIS Simulator - Mobile / Cross-Platform Web UI Companion
Runs on Android (Termux + Chrome), Windows, macOS, and Linux using only Python stdlib.
Supports TCP Client / TCP Server over Wi-Fi, ASTM/HL7/Raw framing, Sample Presets,
and automatic GitHub Release updates + Changelog.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_VERSION = "1.0.6"
GITHUB_REPO = "ShivarenP/PYTHON"
PORT = 8080

BASE_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = BASE_DIR / "saved_samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# Control bytes
ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
EOT = b"\x04"
STX = b"\x02"
ETX = b"\x03"
CR = b"\x0d"
LF = b"\x0a"
VT = b"\x0b"
FS = b"\x1c"

LOGS: list[str] = []
LOG_LOCK = threading.Lock()


def add_log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with LOG_LOCK:
        LOGS.append(f"[{ts}] {msg}")
        if len(LOGS) > 300:
            del LOGS[:-300]


def astm_checksum(frame_body: bytes) -> bytes:
    total = sum(frame_body) & 0xFF
    return f"{total:02X}".encode("ascii")


def build_astm_frames(payload: str) -> list[bytes]:
    lines = [ln.strip() for ln in payload.replace("\r\n", "\n").split("\n") if ln.strip()]
    frames: list[bytes] = []
    for idx, line in enumerate(lines, start=1):
        fn = str(idx % 8).encode("ascii")
        body = fn + line.encode("utf-8") + CR + ETX
        csum = astm_checksum(body)
        frames.append(STX + body + csum + CR + LF)
    return frames


def build_hl7_frame(payload: str) -> bytes:
    normalized = "\r".join(ln.strip() for ln in payload.replace("\r\n", "\n").split("\n") if ln.strip()) + "\r"
    return VT + normalized.encode("utf-8") + FS + CR


def send_tcp_message(host: str, port: int, protocol: str, payload: str, timeout: float = 5.0) -> dict:
    add_log(f"Connecting to {host}:{port} [{protocol}]...")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if protocol == "ASTM":
                sock.sendall(ENQ)
                add_log("TX -> <ENQ>")
                resp = sock.recv(64)
                add_log(f"RX <- {resp!r}")
                if ACK not in resp:
                    return {"ok": False, "error": f"Expected ACK after ENQ, got {resp!r}"}
                for frame in build_astm_frames(payload):
                    sock.sendall(frame)
                    add_log(f"TX -> {frame!r}")
                    ack = sock.recv(64)
                    add_log(f"RX <- {ack!r}")
                    if ACK not in ack:
                        return {"ok": False, "error": f"Frame NAK/rejected: {ack!r}"}
                sock.sendall(EOT)
                add_log("TX -> <EOT>")
                return {"ok": True, "status": "ASTM transmission complete (ACK verified)"}

            elif protocol == "HL7":
                msg = build_hl7_frame(payload)
                sock.sendall(msg)
                add_log(f"TX -> MLLP ({len(msg)} bytes)")
                try:
                    reply = sock.recv(4096)
                    add_log(f"RX <- {reply.decode('utf-8', errors='replace')!r}")
                except socket.timeout:
                    add_log("RX <- (no HL7 ACK within timeout)")
                return {"ok": True, "status": "HL7 MLLP message pushed"}

            else:  # RAW
                raw = payload.replace("\\r", "\r").replace("\\n", "\n").encode("utf-8")
                sock.sendall(raw)
                add_log(f"TX -> RAW ({len(raw)} bytes)")
                try:
                    reply = sock.recv(4096)
                    if reply:
                        add_log(f"RX <- {reply!r}")
                except socket.timeout:
                    pass
                return {"ok": True, "status": "Raw payload sent"}
    except Exception as exc:
        add_log(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}


def _parse_semver(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def check_github_update() -> dict:
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(
        api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Chiron-Mobile-Sim"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name", "")).strip()
        changelog = str(data.get("body", "")).strip() or "No release notes."
        py_url = ""
        for asset in data.get("assets", []):
            if str(asset.get("name", "")).lower() == "chiron_mobile.py":
                py_url = str(asset.get("browser_download_url", ""))
                break
        has_update = bool(tag and _parse_semver(tag) > _parse_semver(APP_VERSION))
        return {
            "has_update": has_update,
            "current_version": f"v{APP_VERSION}",
            "latest_version": tag,
            "changelog": changelog,
            "py_url": py_url,
        }
    except Exception as exc:
        return {"has_update": False, "error": str(exc)}


def apply_mobile_update(py_url: str) -> dict:
    if not py_url:
        return {"ok": False, "error": "No chiron_mobile.py asset attached to latest GitHub release."}
    try:
        req = urllib.request.Request(py_url, headers={"User-Agent": "Chiron-Mobile-Sim"})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            new_code = resp.read()
        target = Path(__file__).resolve()
        tmp = target.with_suffix(".py.new")
        tmp.write_bytes(new_code)
        tmp.replace(target)
        threading.Timer(0.8, lambda: os.execv(sys.executable, [sys.executable, str(target)])).start()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


HTML_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chiron LIS Mobile Simulator (v{APP_VERSION})</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 14px; background: #0f172a; color: #f1f5f9; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 14px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.3); }}
  h1 {{ font-size: 1.15rem; margin: 0 0 8px 0; color: #38bdf8; }}
  label {{ display: block; font-size: .82rem; color: #94a3b8; margin: 8px 0 4px; }}
  input, select, textarea {{ width: 100%; box-sizing: border-box; background: #0f172a; color: #f8fafc; border: 1px solid #334155; border-radius: 8px; padding: 10px; font-size: .95rem; }}
  textarea {{ font-family: monospace; min-height: 150px; }}
  .row {{ display: flex; gap: 8px; }}
  .row > div {{ flex: 1; }}
  button {{ width: 100%; padding: 12px; border: none; border-radius: 8px; font-weight: 700; font-size: .95rem; cursor: pointer; margin-top: 10px; }}
  .btn-primary {{ background: #0284c7; color: white; }}
  .btn-preset {{ background: #334155; color: #e2e8f0; margin-top: 6px; padding: 8px; font-size: .85rem; }}
  .btn-update {{ background: #dc2626; color: white; }}
  pre {{ background: #020617; color: #22c55e; padding: 10px; border-radius: 8px; font-size: .78rem; max-height: 220px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }}
  #updateBanner {{ display: none; border: 2px solid #ef4444; background: #450a0a; }}
</style>
</head>
<body>
  <div id="updateBanner" class="card">
    <h1 style="color:#fca5a5;">Mandatory Update Available (<span id="newVer"></span>)</h1>
    <pre id="changelogBox" style="color:#fecaca; max-height:120px;"></pre>
    <button class="btn-update" onclick="installUpdate()">Install Update & Restart</button>
  </div>

  <div class="card">
    <h1>Chiron LIS Mobile Simulator (v{APP_VERSION})</h1>
    <div class="row">
      <div>
        <label>Target Host / PC IP</label>
        <input id="host" value="172.16.106.181" placeholder="192.168.1.50">
      </div>
      <div>
        <label>Port</label>
        <input id="port" type="number" value="7010">
      </div>
    </div>
    <label>Protocol</label>
    <select id="protocol">
      <option value="ASTM">ASTM E1381 / E1394 (ENQ/STX/ETX/EOT)</option>
      <option value="HL7">HL7 v2.x (MLLP)</option>
      <option value="RAW">Raw Socket</option>
    </select>

    <div class="row">
      <div><button class="btn-preset" onclick="loadPreset('astm')">Load ASTM Preset</button></div>
      <div><button class="btn-preset" onclick="loadPreset('hl7')">Load HL7 Preset</button></div>
    </div>

    <label>Message Payload</label>
    <textarea id="payload">H|\\^&|||CHIRON^SIM|||||||P|1
P|1||PAT001||DOE^JOHN||19850412|M
O|1|TUBE001||^^^GLU\\^^^NA\\^^^K|R
R|1|^^^GLU|5.4|mmol/L|3.9-5.8|N||F
R|2|^^^NA|140|mmol/L|135-145|N||F
R|3|^^^K|4.2|mmol/L|3.5-5.1|N||F
L|1|N</textarea>

    <button class="btn-primary" onclick="pushMessage()">Push to LIS / Chiron</button>
  </div>

  <div class="card">
    <h1>Live Transport Log</h1>
    <pre id="logs">Ready...</pre>
  </div>

<script>
let pendingPyUrl = "";

function loadPreset(type) {{
  if (type === 'astm') {{
    document.getElementById('protocol').value = 'ASTM';
    document.getElementById('payload').value =
`H|\\\\^&|||CHIRON^SIM|||||||P|1
P|1||PAT001||DOE^JOHN||19850412|M
O|1|TUBE001||^^^GLU\\\\^^^NA\\\\^^^K|R
R|1|^^^GLU|5.4|mmol/L|3.9-5.8|N||F
R|2|^^^NA|140|mmol/L|135-145|N||F
R|3|^^^K|4.2|mmol/L|3.5-5.1|N||F
L|1|N`;
  }} else {{
    document.getElementById('protocol').value = 'HL7';
    document.getElementById('payload').value =
`MSH|^~\\\\&|CHIRON|LAB|LIS|HOSP|20261119130000||ORU^R01|MSG001|P|2.5
PID|1||PAT001||DOE^JOHN||19850412|M
OBR|1||TUBE001|CHEM^Chemistry Panel
OBX|1|NM|GLU^Glucose||5.4|mmol/L|3.9-5.8|N|||F`;
  }}
}}

async function pushMessage() {{
  const body = {{
    host: document.getElementById('host').value.trim(),
    port: parseInt(document.getElementById('port').value, 10),
    protocol: document.getElementById('protocol').value,
    payload: document.getElementById('payload').value
  }};
  const res = await fetch('/api/send', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  const data = await res.json();
  refreshLogs();
  if (!data.ok) alert('Error: ' + data.error);
}}

async function refreshLogs() {{
  const res = await fetch('/api/logs');
  const data = await res.json();
  const box = document.getElementById('logs');
  box.textContent = data.logs.join('\\n') || 'Waiting for activity...';
  box.scrollTop = box.scrollHeight;
}}

async function checkUpdates() {{
  const res = await fetch('/api/check_update');
  const data = await res.json();
  if (data.has_update) {{
    pendingPyUrl = data.py_url;
    document.getElementById('newVer').textContent = data.latest_version;
    document.getElementById('changelogBox').textContent = data.changelog;
    document.getElementById('updateBanner').style.display = 'block';
  }}
}}

async function installUpdate() {{
  const res = await fetch('/api/apply_update', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{py_url: pendingPyUrl}})
  }});
  const data = await res.json();
  if (data.ok) {{
    alert('Updated! Reloading page in 2 seconds...');
    setTimeout(() => location.reload(), 2000);
  }} else {{
    alert('Update failed: ' + data.error);
  }}
}}

setInterval(refreshLogs, 1500);
checkUpdates();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj: dict, code: int = 200) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/":
            raw = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/logs":
            with LOG_LOCK:
                self._send_json({"logs": list(LOGS)})
        elif self.path == "/api/check_update":
            self._send_json(check_github_update())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if self.path == "/api/send":
            res = send_tcp_message(
                host=body.get("host", "127.0.0.1"),
                port=int(body.get("port", 7010)),
                protocol=body.get("protocol", "ASTM"),
                payload=body.get("payload", ""),
            )
            self._send_json(res)
        elif self.path == "/api/apply_update":
            self._send_json(apply_mobile_update(body.get("py_url", "")))
        else:
            self.send_error(404)


if __name__ == "__main__":
    add_log(f"Chiron LIS Mobile Simulator v{APP_VERSION} started on http://127.0.0.1:{PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass