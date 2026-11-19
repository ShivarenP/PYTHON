import json
import re
import socket
import threading
import urllib.request
from datetime import datetime

import flet as ft

APP_VERSION = "1.1.0"
GITHUB_REPO = "ShivarenP/PYTHON"

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

ASTM_PRESET = """H|\\^&|||CHIRON^SIM|||||||P|1
P|1||PAT001||DOE^JOHN||19850412|M
O|1|TUBE001||^^^GLU\\^^^NA\\^^^K|R
R|1|^^^GLU|5.4|mmol/L|3.9-5.8|N||F
R|2|^^^NA|140|mmol/L|135-145|N||F
R|3|^^^K|4.2|mmol/L|3.5-5.1|N||F
L|1|N"""

HL7_PRESET = """MSH|^~\\&|CHIRON|LAB|LIS|HOSP|20261119130000||ORU^R01|MSG001|P|2.5
PID|1||PAT001||DOE^JOHN||19850412|M
OBR|1||TUBE001|CHEM^Chemistry Panel
OBX|1|NM|GLU^Glucose||5.4|mmol/L|3.9-5.8|N|||F"""


def astm_checksum(frame_body: bytes) -> bytes:
    return f"{sum(frame_body) & 0xFF:02X}".encode("ascii")


def build_astm_frames(payload: str) -> list[bytes]:
    lines = [ln.strip() for ln in payload.replace("\r\n", "\n").split("\n") if ln.strip()]
    frames: list[bytes] = []
    for idx, line in enumerate(lines, start=1):
        fn = str(idx % 8).encode("ascii")
        body = fn + line.encode("utf-8") + CR + ETX
        frames.append(STX + body + astm_checksum(body) + CR + LF)
    return frames


def build_hl7_frame(payload: str) -> bytes:
    normalized = "\r".join(ln.strip() for ln in payload.replace("\r\n", "\n").split("\n") if ln.strip()) + "\r"
    return VT + normalized.encode("utf-8") + FS + CR


def _parse_semver(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def check_github_release() -> dict | None:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Chiron-Android-Sim"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name", "")).strip()
        if not tag or _parse_semver(tag) <= _parse_semver(APP_VERSION):
            return None
        apk_url = ""
        for asset in data.get("assets", []):
            if str(asset.get("name", "")).lower().endswith(".apk"):
                apk_url = str(asset.get("browser_download_url", ""))
                break
        return {
            "version": tag,
            "changelog": str(data.get("body", "")).strip() or "New version available on GitHub.",
            "apk_url": apk_url or f"https://github.com/{GITHUB_REPO}/releases/latest",
        }
    except Exception:
        return None


def main(page: ft.Page):
    page.title = f"Chiron LIS Simulator (v{APP_VERSION})"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0f172a"
    page.padding = 14
    page.scroll = ft.ScrollMode.AUTO

    log_lines: list[str] = [f"[{datetime.now().strftime('%H:%M:%S')}] Ready (v{APP_VERSION})"]

    log_text = ft.Text(
        value="\n".join(log_lines),
        font_family="monospace",
        size=12,
        color="#22c55e",
        selectable=True,
    )

    def append_log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] {msg}")
        if len(log_lines) > 200:
            del log_lines[:-200]
        log_text.value = "\n".join(log_lines)
        page.update()

    host_field = ft.TextField(label="PC / LIS IP", value="172.16.106.181", expand=2, dense=True)
    port_field = ft.TextField(label="Port", value="7010", keyboard_type=ft.KeyboardType.NUMBER, expand=1, dense=True)

    protocol_dd = ft.Dropdown(
        label="Protocol",
        value="ASTM",
        options=[
            ft.dropdown.Option("ASTM", "ASTM E1381 / E1394"),
            ft.dropdown.Option("HL7", "HL7 v2.x (MLLP)"),
            ft.dropdown.Option("RAW", "Raw Socket"),
        ],
        dense=True,
    )

    payload_field = ft.TextField(
        label="Message Payload",
        value=ASTM_PRESET,
        multiline=True,
        min_lines=7,
        max_lines=11,
        text_style=ft.TextStyle(font_family="monospace", size=12),
    )

    def load_astm(_):
        protocol_dd.value = "ASTM"
        payload_field.value = ASTM_PRESET
        page.update()

    def load_hl7(_):
        protocol_dd.value = "HL7"
        payload_field.value = HL7_PRESET
        page.update()

    send_btn = ft.FilledButton(
        "Push to LIS / Chiron",
        icon=ft.Icons.SEND,
        style=ft.ButtonStyle(bgcolor="#0284c7", color="white"),
        height=46,
    )

    def on_send(_):
        host = (host_field.value or "").strip()
        try:
            port = int((port_field.value or "7010").strip())
        except ValueError:
            append_log("ERROR: Invalid port number")
            return
        proto = protocol_dd.value or "ASTM"
        payload = payload_field.value or ""

        send_btn.disabled = True
        page.update()

        def worker():
            append_log(f"Connecting to {host}:{port} [{proto}]...")
            try:
                with socket.create_connection((host, port), timeout=5.0) as sock:
                    sock.settimeout(5.0)
                    if proto == "ASTM":
                        sock.sendall(ENQ)
                        append_log("TX -> <ENQ>")
                        resp = sock.recv(64)
                        append_log(f"RX <- {resp!r}")
                        if ACK not in resp:
                            append_log("ERROR: Expected ACK after ENQ")
                            return
                        for frame in build_astm_frames(payload):
                            sock.sendall(frame)
                            append_log(f"TX -> {frame!r}")
                            ack = sock.recv(64)
                            append_log(f"RX <- {ack!r}")
                            if ACK not in ack:
                                append_log("ERROR: Frame rejected (NAK)")
                                return
                        sock.sendall(EOT)
                        append_log("TX -> <EOT> (Complete)")
                    elif proto == "HL7":
                        msg = build_hl7_frame(payload)
                        sock.sendall(msg)
                        append_log(f"TX -> MLLP ({len(msg)} bytes)")
                        try:
                            reply = sock.recv(4096)
                            append_log(f"RX <- {reply.decode('utf-8', errors='replace')!r}")
                        except socket.timeout:
                            append_log("RX <- (no HL7 ACK)")
                    else:
                        raw = payload.replace("\\r", "\r").replace("\\n", "\n").encode("utf-8")
                        sock.sendall(raw)
                        append_log(f"TX -> RAW ({len(raw)} bytes)")
            except Exception as exc:
                append_log(f"ERROR: {exc}")
            finally:
                send_btn.disabled = False
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    send_btn.on_click = on_send

    # Update Dialog
    def show_update_dialog(release: dict):
        def open_apk_download(_):
            page.launch_url(release["apk_url"])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Update Available ({release['version']})"),
            content=ft.Column(
                [
                    ft.Text(f"Installed: v{APP_VERSION} -> Latest: {release['version']}", weight=ft.FontWeight.BOLD),
                    ft.Container(
                        content=ft.Text(release["changelog"], size=12),
                        bgcolor="#020617",
                        padding=10,
                        border_radius=8,
                    ),
                ],
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.FilledButton(
                    "Download & Install APK",
                    icon=ft.Icons.DOWNLOAD,
                    on_click=open_apk_download,
                )
            ],
        )
        page.open(dlg)

    def check_updates_bg():
        rel = check_github_release()
        if rel:
            show_update_dialog(rel)

    page.add(
        ft.Container(
            bgcolor="#1e293b",
            border_radius=12,
            padding=14,
            content=ft.Column(
                [
                    ft.Text(f"Chiron LIS Simulator (v{APP_VERSION})", size=18, weight=ft.FontWeight.BOLD, color="#38bdf8"),
                    ft.Row([host_field, port_field]),
                    protocol_dd,
                    ft.Row(
                        [
                            ft.OutlinedButton("ASTM Preset", on_click=load_astm, expand=True),
                            ft.OutlinedButton("HL7 Preset", on_click=load_hl7, expand=True),
                        ]
                    ),
                    payload_field,
                    send_btn,
                ],
                spacing=10,
            ),
        ),
        ft.Container(
            bgcolor="#1e293b",
            border_radius=12,
            padding=14,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Live Transport Log", size=15, weight=ft.FontWeight.BOLD, color="#38bdf8", expand=True),
                            ft.TextButton("Clear", on_click=lambda _: (log_lines.clear(), append_log("Cleared"))),
                        ]
                    ),
                    ft.Container(
                        content=log_text,
                        bgcolor="#020617",
                        border_radius=8,
                        padding=10,
                        height=200,
                    ),
                ],
                spacing=8,
            ),
        ),
    )

    threading.Thread(target=check_updates_bg, daemon=True).start()


ft.app(main)