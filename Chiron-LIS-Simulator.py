from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import uuid
import sys
import subprocess
import sys
import urllib.request
import os

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SAMPLES_DIR = BASE_DIR / "saved_samples"
TUBE_MEMORY_FILE = BASE_DIR / "tube_memory.json"

APP_VERSION = "1.0.6"
GITHUB_REPO = "ShivarenP/chiron-lis-simulator"  # <-- Change this!

COMMON_TUBES = [
    "EDTA1",
    "SST1",
    "FLU1",
    "CIT1",
    "HEP1",
    "URINE1",
    "PLAIN1",
    "(Default Barcode)",
]

DEFAULT_CODE_MAP = {
    "hb": "HGB",
    "haemoglobin": "HGB",
    "hemoglobin": "HGB",
    "platelets": "PLT",
    "rbc": "RBC",
    "red blood cells": "RBC",
    "hct": "HCT",
    "haematocrit": "HCT",
    "hematocrit": "HCT",
    "mcv": "MCV",
    "mch": "MCH",
    "mchc": "MCHC",
    "rdw": "RDW",
    "wbc": "WBC",
    "white blood cells": "WBC",
    "neutrophil count": "NEU#",
    "neutrophil percentage": "NEU%",
    "lymphocyte count": "LYM#",
    "lymphocyte percentage": "LYM%",
    "monocyte count": "MONO#",
    "monocyte percentage": "MONO%",
    "eosinophil count": "EOS#",
    "eosinophil percentage": "EOS%",
    "basophil count": "BASO#",
    "basophil percentage": "BASO%",
}

PROTOCOLS = {
    "1": "ASTM (Standard E1381/E1394)",
    "2": "HL7 (v2.x MLLP)",
    "3": "Beckman AU Native ASCII",
    "4": "Cobas e411 (Elecsys ASTM)",
}

PROTOCOL_BY_LABEL = {v: k for k, v in PROTOCOLS.items()}

ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
EOT = b"\x04"


# =====================================================================
# TUBE MEMORY & BARCODE RESOLUTION ENGINE
# =====================================================================

def load_tube_memory() -> dict[str, str]:
    if TUBE_MEMORY_FILE.exists():
        try:
            data = json.loads(TUBE_MEMORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).upper(): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def save_tube_memory(memory: dict[str, str]) -> None:
    try:
        TUBE_MEMORY_FILE.write_text(json.dumps(memory, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def split_base_and_tube(default_sample_id: str) -> tuple[str, str]:
    """Splits '85308824-EDTA1' -> ('85308824', 'EDTA1'). If no '-', returns (sid, 'EDTA1')."""
    sid = default_sample_id.strip()
    if "-" in sid:
        base, tube = sid.rsplit("-", 1)
        return base.strip(), tube.strip()
    return sid, "EDTA1"


def resolve_barcode(default_sample_id: str, override: str) -> str:
    ov = override.strip()
    if not ov or ov == "(Default Barcode)":
        return default_sample_id.strip()
    if "-" in ov or not default_sample_id.strip():
        return ov
    base = default_sample_id.strip().rsplit("-", 1)[0]
    return f"{base}-{ov}"


def calculate_astm_checksum(frame_body: str) -> str:
    return f"{sum(ord(c) for c in frame_body) % 256:02X}"


def escape_hercules_macros(text: str) -> str:
    return text.replace("#", "##").replace("$", "$$").replace("<", "<<")


def normalize_host_code(raw_name: str) -> str:
    cleaned = re.sub(r"\(.*?\)", "", raw_name).strip()
    return DEFAULT_CODE_MAP.get(cleaned.lower(), cleaned)


def parse_smart_line(line: str) -> tuple[str, str, str, str] | None:
    """Returns (code, value, unit, barcode_override)."""
    cleaned = line.strip()
    if not cleaned:
        return None

    if "\t" in cleaned:
        cols = [c.strip() for c in cleaned.split("\t")]
        if len(cols) >= 3:
            code = normalize_host_code(cols[0])
            unit = cols[1]
            val_col = cols[2]
            num_match = re.match(r"^([+-]?\d+(?:\.\d+)?)\s+", val_col)
            val = num_match.group(1) if num_match else val_col
            barcode_ov = cols[3] if len(cols) > 3 else ""
            return (code, val, unit, barcode_ov)

    for delim in ("|", ","):
        if delim in cleaned:
            parts = [p.strip() for p in cleaned.split(delim)]
            code = normalize_host_code(parts[0])
            val = parts[1] if len(parts) > 1 else ""
            unit = parts[2] if len(parts) > 2 else ""
            barcode_ov = parts[3] if len(parts) > 3 else ""
            return (code, val, unit, barcode_ov)

    cleaned = cleaned.replace("=", " ")
    parts = cleaned.split()
    code = normalize_host_code(parts[0])
    val = parts[1] if len(parts) > 1 else ""
    unit = parts[2] if len(parts) > 2 else ""
    barcode_ov = parts[3] if len(parts) > 3 else ""
    return (code, val, unit, barcode_ov)


def parse_results_block(text: str, non_numeric_mode: str = "skip") -> list[tuple[str, str, str, str]]:
    results: list[tuple[str, str, str, str]] = []
    for raw_line in text.splitlines():
        sub_lines = raw_line.split(";") if (";" in raw_line and "\t" not in raw_line) else [raw_line]
        for line in sub_lines:
            parsed = parse_smart_line(line)
            if not parsed or not parsed[0]:
                continue
            code, val, unit, barcode_ov = parsed
            try:
                float(val)
            except ValueError:
                if non_numeric_mode == "skip":
                    continue
                if non_numeric_mode == "dashes":
                    val = "-----"
            results.append((code, val, unit, barcode_ov))
    return results


def group_by_barcode(
    default_sample_id: str, results: list[tuple[str, str, str, str]]
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for code, val, unit, barcode_ov in results:
        resolved = resolve_barcode(default_sample_id, barcode_ov)
        grouped[resolved].append((code, val, unit))
    return list(grouped.items())


# =====================================================================
# PER-BARCODE PROTOCOL BUILDERS (Each tube = Orders[0].Order.SampleID)
# =====================================================================

def wrap_astm_records(records: list[str]) -> list[bytes]:
    frames: list[bytes] = []
    for i, rec in enumerate(records, start=1):
        frame_num = i % 8
        body = f"{frame_num}{rec}\r\x03"
        cs = calculate_astm_checksum(body)
        frames.append(f"\x02{body}{cs}\r\n".encode("ascii"))
    return frames


def build_single_astm_frames(
    source_uid: str, barcode: str, items: list[tuple[str, str, str]], use_carets: bool = False
) -> list[bytes]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    prefix = "^^^" if use_carets else ""
    test_ids = "\\".join(f"{prefix}{c}" for c, _, _ in items)
    records = [
        rf"H|\^&|||{source_uid}|||||||P|1|{ts}",
        "P|1",
        f"O|1|{barcode}||{test_ids}|||||||N",
    ]
    for res_seq, (code, val, unit) in enumerate(items, start=1):
        records.append(f"R|{res_seq}|{prefix}{code}|{val}|{unit}||N||F||||{ts}")
    records.append("L|1|N")
    return wrap_astm_records(records)


def build_single_cobas_frames(
    source_uid: str, barcode: str, items: list[tuple[str, str, str]]
) -> list[bytes]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    test_ids = "\\".join(f"^^^{c}/1" for c, _, _ in items)
    records = [
        rf"H|\^&|||{source_uid}^Elecsys||||||RSUPL^REAL|P|1|{ts}",
        "P|1",
        f"O|1|{barcode}|0^0^1^^SAMPLE^SC|{test_ids}|||||||SAMPLE||||SAMPLE||||||||||F",
    ]
    for res_seq, (code, val, unit) in enumerate(items, start=1):
        records.append(f"R|{res_seq}|^^^{code}/1|{val}|{unit}||N||F||ADMIN||{ts}")
    records.append("L|1|N")
    return wrap_astm_records(records)


def build_single_hl7_message(
    source_uid: str, barcode: str, items: list[tuple[str, str, str]]
) -> bytes:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    ctrl_id = uuid.uuid4().hex[:10].upper()
    segments = [
        rf"MSH|^~\&|{source_uid}|LAB|CHIRON|LIS|{ts}||ORU^R01|{ctrl_id}|P|2.5",
        "PID|1",
        f"ORC|RE|{barcode}",
        f"OBR|1|{barcode}||{source_uid}^PANEL|||{ts}",
    ]
    for res_seq, (code, val, unit) in enumerate(items, start=1):
        try:
            float(val)
            vtype = "NM"
        except ValueError:
            vtype = "ST"
        segments.append(f"OBX|{res_seq}|{vtype}|{code}^{code}||{val}|{unit}|||||F|||{ts}")
    return b"\x0b" + ("\r".join(segments) + "\r").encode("ascii") + b"\x1c\r"


def build_single_beckman_frame(barcode: str, items: list[tuple[str, str, str]]) -> bytes:
    padded_sample = f"{barcode:<26}"[:26]
    demographics_pad = " " * 126
    blocks = [f"{c:>3}"[:3] + f"{v:>6}"[:6] + " " for c, v, _ in items]
    payload = f"D 01000101 0001{padded_sample}00001{demographics_pad}E" + "".join(blocks)
    return b"\x02" + payload.encode("ascii") + b"\x03"


def build_payload(
    proto: str, source_uid: str, default_sample_id: str, results: list[tuple[str, str, str, str]], use_carets: bool
) -> list[tuple[str, list[bytes] | bytes]]:
    """Returns a list of (barcode, payload) batches so every barcode is sent as Orders[0]."""
    batches: list[tuple[str, list[bytes] | bytes]] = []
    for barcode, items in group_by_barcode(default_sample_id, results):
        if proto == "1":
            batches.append((barcode, build_single_astm_frames(source_uid, barcode, items, use_carets)))
        elif proto == "2":
            batches.append((barcode, build_single_hl7_message(source_uid, barcode, items)))
        elif proto == "3":
            batches.append((barcode, build_single_beckman_frame(barcode, items)))
        else:
            batches.append((barcode, build_single_cobas_frames(source_uid, barcode, items)))
    return batches


# =====================================================================
# TRANSPORTS
# =====================================================================

class TcpClientSession:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

    def open(self, log_fn) -> None:
        if self.sock is not None:
            return
        log_fn(f"[*] Connecting TCP Client -> {self.host}:{self.port}...")
        self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.sock.settimeout(3.0)
        time.sleep(0.3)

    def reconnect(self, log_fn) -> None:
        self.close()
        time.sleep(1.5)
        self.open(log_fn)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self, size: int = 1) -> bytes:
        try:
            data = self.sock.recv(size)
            if data == b"":
                self.close()
            return data
        except socket.timeout:
            return b""

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# =====================================================================
# PRE-FLIGHT TUBE ASSIGNMENT MODAL
# =====================================================================

class TubeAssignmentDialog(tk.Toplevel):
    """
    Modal dialog allowing per-test tube selection (EDTA1, SST1, FLU1, etc.)
    and selective inclusion before transmitting a batch.
    """

    def __init__(
        self,
        parent: tk.Tk,
        default_sample_id: str,
        results: list[tuple[str, str, str, str]],
        tube_memory: dict[str, str],
    ):
        super().__init__(parent)
        self.title("Assign Tubes & Review Batch Barcodes")
        self.geometry("740x480")
        self.minsize(660, 380)
        self.transient(parent)
        self.grab_set()

        self.default_sample_id = default_sample_id.strip()
        self.base_req, self.default_tube = split_base_and_tube(self.default_sample_id)
        self.tube_memory = tube_memory
        self.confirmed_results: list[tuple[str, str, str, str]] | None = None
        self.row_widgets: list[dict] = []

        self._build_ui(results)
        self.bind("<Return>", lambda _e: self._on_confirm())
        self.bind("<Escape>", lambda _e: self.destroy())

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_ui(self, results: list[tuple[str, str, str, str]]) -> None:
        top_bar = ttk.Frame(self, padding=(12, 10, 12, 6))
        top_bar.pack(fill=tk.X)

        ttk.Label(
            top_bar,
            text=f"Base Barcode: {self.default_sample_id or '(None)'}",
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT)

        bulk_frame = ttk.Frame(top_bar)
        bulk_frame.pack(side=tk.RIGHT)
        ttk.Label(bulk_frame, text="Set all checked to:").pack(side=tk.LEFT, padx=(0, 6))
        self.bulk_tube_var = tk.StringVar(value=self.default_tube or "EDTA1")
        ttk.Combobox(
            bulk_frame, textvariable=self.bulk_tube_var, values=COMMON_TUBES, width=16
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bulk_frame, text="Apply", command=self._apply_bulk_tube).pack(side=tk.LEFT)

        container = ttk.Frame(self, padding=(12, 4, 12, 4))
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(header, text="Send", width=6, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Test Code", width=14, font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="Value", width=12, font=("Segoe UI", 9, "bold")).grid(row=0, column=2, sticky="w")
        ttk.Label(header, text="Unit", width=12, font=("Segoe UI", 9, "bold")).grid(row=0, column=3, sticky="w")
        ttk.Label(header, text="Tube / Barcode", width=18, font=("Segoe UI", 9, "bold")).grid(row=0, column=4, sticky="w")
        ttk.Label(header, text="Resolved Barcode", width=24, font=("Segoe UI", 9, "bold")).grid(row=0, column=5, sticky="w")

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        scroll_inner = ttk.Frame(canvas)

        scroll_inner.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for idx, (code, val, unit, explicit_ov) in enumerate(results):
            include_var = tk.BooleanVar(value=True)
            remembered = self.tube_memory.get(code.upper(), "")
            initial_tube = explicit_ov or remembered or self.default_tube or "EDTA1"

            tube_var = tk.StringVar(value=initial_tube)
            preview_var = tk.StringVar(value=resolve_barcode(self.default_sample_id, initial_tube))

            ttk.Checkbutton(scroll_inner, variable=include_var).grid(row=idx, column=0, padx=(4, 14), pady=3, sticky="w")
            ttk.Label(scroll_inner, text=code, width=14, font=("Consolas", 10, "bold")).grid(row=idx, column=1, pady=3, sticky="w")
            ttk.Label(scroll_inner, text=val, width=12, font=("Consolas", 10)).grid(row=idx, column=2, pady=3, sticky="w")
            ttk.Label(scroll_inner, text=unit, width=12, font=("Consolas", 9)).grid(row=idx, column=3, pady=3, sticky="w")

            combo = ttk.Combobox(scroll_inner, textvariable=tube_var, values=COMMON_TUBES, width=16)
            combo.grid(row=idx, column=4, padx=(0, 12), pady=3, sticky="w")

            lbl_prev = ttk.Label(scroll_inner, textvariable=preview_var, width=26, font=("Consolas", 9), foreground="#0b5394")
            lbl_prev.grid(row=idx, column=5, pady=3, sticky="w")

            def _make_updater(t_var=tube_var, p_var=preview_var):
                def _update(*_args):
                    p_var.set(resolve_barcode(self.default_sample_id, t_var.get()))
                return _update

            updater = _make_updater()
            tube_var.trace_add("write", updater)
            combo.bind("<<ComboboxSelected>>", updater)

            self.row_widgets.append(
                {
                    "include": include_var,
                    "code": code,
                    "value": val,
                    "unit": unit,
                    "tube": tube_var,
                }
            )

        footer = ttk.Frame(self, padding=(12, 8, 12, 12))
        footer.pack(fill=tk.X)

        sel_btns = ttk.Frame(footer)
        sel_btns.pack(side=tk.LEFT)
        ttk.Button(sel_btns, text="Check All", command=lambda: self._set_all_checks(True)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(sel_btns, text="Uncheck All", command=lambda: self._set_all_checks(False)).pack(side=tk.LEFT)

        act_btns = ttk.Frame(footer)
        act_btns.pack(side=tk.RIGHT)
        ttk.Button(act_btns, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(act_btns, text="Confirm & Push Batch", command=self._on_confirm).pack(side=tk.LEFT)

    def _set_all_checks(self, state: bool) -> None:
        for row in self.row_widgets:
            row["include"].set(state)

    def _apply_bulk_tube(self) -> None:
        chosen = self.bulk_tube_var.get().strip()
        if not chosen:
            return
        for row in self.row_widgets:
            if row["include"].get():
                row["tube"].set(chosen)

    def _on_confirm(self) -> None:
        selected: list[tuple[str, str, str, str]] = []
        for row in self.row_widgets:
            code = row["code"]
            tube = row["tube"].get().strip()
            if tube and tube != "(Default Barcode)":
                self.tube_memory[code.upper()] = tube
            if row["include"].get():
                ov = "" if tube == "(Default Barcode)" else tube
                selected.append((code, row["value"], row["unit"], ov))

        if not selected:
            messagebox.showwarning("Nothing Selected", "Check at least one test row to transmit.", parent=self)
            return

        save_tube_memory(self.tube_memory)
        self.confirmed_results = selected
        self.destroy()


# =====================================================================
# DESKTOP GUI APPLICATION
# =====================================================================

# =====================================================================
# FORCED AUTO-UPDATER & CHANGELOG DIALOG (GitHub Releases)
# =====================================================================

def _parse_semver(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def fetch_latest_github_release(repo: str) -> dict | None:
    if not repo or "YOUR_GITHUB_USERNAME" in repo:
        return None
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Chiron-LIS-Simulator-Updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name", "")).strip()
        changelog = str(data.get("body", "")).strip() or "No release notes provided."
        assets = data.get("assets", [])
        exe_url = ""
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if name.endswith(".exe"):
                exe_url = str(asset.get("browser_download_url", ""))
                break
        if tag and exe_url and _parse_semver(tag) > _parse_semver(APP_VERSION):
            return {
                "version": tag,
                "changelog": changelog,
                "download_url": exe_url,
            }
    except Exception:
        pass
    return None


class ForcedUpdateDialog(tk.Toplevel):
    """Non-dismissible modal that displays the changelog and forces update installation."""

    def __init__(self, parent: tk.Tk, release_info: dict):
        super().__init__(parent)
        self.title(f"Mandatory Update Required ({release_info['version']})")
        self.geometry("560x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        self.release_info = release_info
        self._build_ui()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.after(300, self._start_download)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text=f"A new version ({self.release_info['version']}) is required",
            font=("Segoe UI", 12, "bold"),
            foreground="#b71c1c",
        ).pack(anchor="w")

        ttk.Label(
            frame,
            text=f"Installed version: v{APP_VERSION}  →  Latest version: {self.release_info['version']}",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 10))

        ttk.Label(frame, text="Changelog / What's New:", font=("Segoe UI", 9, "bold")).pack(anchor="w")

        notes_box = scrolledtext.ScrolledText(
            frame, height=12, font=("Segoe UI", 9), wrap=tk.WORD, bg="#f8f9fa"
        )
        notes_box.pack(fill=tk.BOTH, expand=True, pady=(4, 12))
        notes_box.insert("1.0", self.release_info["changelog"])
        notes_box.configure(state=tk.DISABLED)

        self.status_var = tk.StringVar(value="Preparing download...")
        ttk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 9)).pack(anchor="w")

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(4, 10))

        self.retry_btn = ttk.Button(frame, text="Retry Download", command=self._start_download)

    def _start_download(self) -> None:
        self.retry_btn.pack_forget()
        self.progress["value"] = 0
        self.status_var.set("Downloading update...")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self) -> None:
        if not getattr(sys, "frozen", False):
            self.after(
                0,
                lambda: self.status_var.set(
                    "Running from .py source — build .exe to test binary self-replacement."
                ),
            )
            return

        current_exe = Path(sys.executable).resolve()
        staged_exe = current_exe.with_name(current_exe.name + ".new")
        url = self.release_info["download_url"]

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Chiron-LIS-Simulator-Updater"})
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with staged_exe.open("wb") as out_f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = min(100, int(downloaded * 100 / total))
                            self.after(
                                0,
                                lambda p=pct, d=downloaded, t=total: (
                                    self.progress.configure(value=p),
                                    self.status_var.set(
                                        f"Downloading update... {p}% ({d // 1024:,} KB / {t // 1024:,} KB)"
                                    ),
                                ),
                            )

            self.after(0, lambda: self.status_var.set("Installing update & restarting..."))
            self.after(400, lambda: self._apply_and_restart(current_exe, staged_exe))

        except Exception as exc:
            self.after(
                0,
                lambda e=exc: (
                    self.status_var.set(f"Download failed: {e}"),
                    self.retry_btn.pack(anchor="e"),
                ),
            )

def _apply_and_restart(self, current_exe: Path, staged_exe: Path) -> None:
        bat_path = current_exe.with_name("_chiron_sim_update.bat")
        bat_script = f"""@echo off
:retry
timeout /t 1 /nobreak >nul
move /y "{staged_exe}" "{current_exe}" >nul 2>&1
if exist "{staged_exe}" goto retry
explorer.exe "{current_exe}"
del "%~f0"
"""
        bat_path.write_text(bat_script, encoding="utf-8")
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("_PYI_", "_MEI", "TCL_", "TK_"))
        }
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            env=clean_env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        os._exit(0)

class LisSimulatorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Universal LIS / Chiron Protocol & Transport Simulator (v{APP_VERSION})")
        self.geometry("1150x760")
        self.minsize(980, 650)

        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        self.tube_memory = load_tube_memory()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.tcp_session: TcpClientSession | None = None
        self.tcp_session_key: tuple[str, int] | None = None
        self.push_counter: dict[str, int] = {}
        self.saved_files: list[Path] = []

        self._build_ui()
        self._refresh_sample_list()
        self.after(100, self._drain_log_queue)
        self.after(500, self._check_forced_update_async)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _check_forced_update_async(self) -> None:
        guard_file = BASE_DIR / ".update_attempted"
        if guard_file.exists():
            try:
                # If an update ran less than 30 seconds ago, skip checking to prevent infinite loops
                if time.time() - guard_file.stat().st_mtime < 30:
                    return
                guard_file.unlink(missing_ok=True)
            except OSError:
                pass

        def _worker():
            release = fetch_latest_github_release(GITHUB_REPO)
            if release:
                try:
                    guard_file.write_text(release["version"], encoding="utf-8")
                except OSError:
                    pass
                self.after(0, lambda r=release: ForcedUpdateDialog(self, r))

        threading.Thread(target=_worker, daemon=True).start()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ---------------- LEFT SIDEBAR: SAVED SAMPLES ----------------
        left_frame = ttk.LabelFrame(main_pane, text="Saved Samples (saved_samples/)", padding=8)
        main_pane.add(left_frame, weight=1)

        self.sample_listbox = tk.Listbox(left_frame, font=("Consolas", 9), activestyle="none")
        self.sample_listbox.pack(fill=tk.BOTH, expand=True)
        self.sample_listbox.bind("<<ListboxSelect>>", self._on_select_saved)

        sb_btns = ttk.Frame(left_frame)
        sb_btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(sb_btns, text="Refresh", command=self._refresh_sample_list).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        ttk.Button(sb_btns, text="Delete", command=self._delete_selected_sample).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        # ---------------- RIGHT WORKSPACE ----------------
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=3)

        # 1. Protocol & Sample Metadata
        meta_frame = ttk.LabelFrame(right_frame, text="1. Analyzer & Default Barcode Configuration", padding=8)
        meta_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(meta_frame, text="Protocol:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.proto_var = tk.StringVar(value=PROTOCOLS["4"])
        self.proto_combo = ttk.Combobox(
            meta_frame, textvariable=self.proto_var, values=list(PROTOCOLS.values()), state="readonly", width=28
        )
        self.proto_combo.grid(row=0, column=1, sticky="w", padx=(0, 12))
        self.proto_combo.bind("<<ComboboxSelected>>", self._on_proto_changed)

        ttk.Label(meta_frame, text="Source UID:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.uid_var = tk.StringVar(value="Cobas411")
        ttk.Entry(meta_frame, textvariable=self.uid_var, width=16).grid(row=0, column=3, sticky="w", padx=(0, 12))

        ttk.Label(meta_frame, text="Default Barcode:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.sample_var = tk.StringVar(value="85308824-EDTA1")
        ttk.Entry(meta_frame, textvariable=self.sample_var, width=20).grid(row=0, column=5, sticky="w")

        opts_row = ttk.Frame(meta_frame)
        opts_row.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))

        self.carets_var = tk.BooleanVar(value=True)
        self.carets_chk = ttk.Checkbutton(opts_row, text="Prefix test codes with '^^^'", variable=self.carets_var)
        self.carets_chk.pack(side=tk.LEFT, padx=(0, 14))

        self.dedup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts_row, text="Auto Dedup-Bypass", variable=self.dedup_var
        ).pack(side=tk.LEFT, padx=(0, 14))

        self.ask_tubes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts_row, text="Assign Tubes pop-up before Push", variable=self.ask_tubes_var
        ).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(opts_row, text="Non-numeric:").pack(side=tk.LEFT, padx=(0, 4))
        self.non_num_var = tk.StringVar(value="skip")
        ttk.Combobox(
            opts_row, textvariable=self.non_num_var, values=["skip", "dashes", "keep"], state="readonly", width=7
        ).pack(side=tk.LEFT)

        self._on_proto_changed()

        # 2. Results Input Area
        res_frame = ttk.LabelFrame(
            right_frame,
            text="2. Results (Smart Paste: Code | Value | Unit  OR  Lab Table)",
            padding=8,
        )
        res_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.results_text = scrolledtext.ScrolledText(res_frame, height=10, font=("Consolas", 10))
        self.results_text.pack(fill=tk.BOTH, expand=True)
        self.results_text.insert(
            "1.0",
            "RBC|12|10^12/L\nWBC|15.8|10^9/L\nHBA1C|23.1|%\nLDL|3.20|mmol/L\n",
        )

        res_toolbar = ttk.Frame(res_frame)
        res_toolbar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(res_toolbar, text="Format / Clean Table", command=self._clean_results_box).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(res_toolbar, text="Save Sample to Library", command=self._save_current_sample).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(res_toolbar, text="Copy Hercules Frames", command=self._copy_hercules).pack(side=tk.LEFT)

        # 3. Transport Settings & Push Controls
        trans_frame = ttk.LabelFrame(right_frame, text="3. Transport & Transmit", padding=8)
        trans_frame.pack(fill=tk.X, pady=(0, 6))

        self.transport_var = tk.StringVar(value="Serial COM")
        ttk.Label(trans_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        t_combo = ttk.Combobox(
            trans_frame,
            textvariable=self.transport_var,
            values=["TCP Client", "TCP Server", "Serial COM"],
            state="readonly",
            width=12,
        )
        t_combo.grid(row=0, column=1, sticky="w", padx=(0, 10))
        t_combo.bind("<<ComboboxSelected>>", self._on_transport_changed)

        ttk.Label(trans_frame, text="Host / IP:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.host_var = tk.StringVar(value="172.16.106.181")
        self.host_entry = ttk.Entry(trans_frame, textvariable=self.host_var, width=16)
        self.host_entry.grid(row=0, column=3, sticky="w", padx=(0, 10))

        ttk.Label(trans_frame, text="Port:").grid(row=0, column=4, sticky="w", padx=(0, 4))
        self.port_var = tk.StringVar(value="7010")
        self.port_entry = ttk.Entry(trans_frame, textvariable=self.port_var, width=7)
        self.port_entry.grid(row=0, column=5, sticky="w", padx=(0, 12))

        ttk.Label(trans_frame, text="COM:").grid(row=0, column=6, sticky="w", padx=(0, 4))
        com_defaults = self._detect_com_ports()
        self.com_var = tk.StringVar(value="COM11" if "COM11" in com_defaults else com_defaults[0])
        self.com_combo = ttk.Combobox(trans_frame, textvariable=self.com_var, values=com_defaults, width=9)
        self.com_combo.grid(row=0, column=7, sticky="w", padx=(0, 8))

        ttk.Label(trans_frame, text="Baud:").grid(row=0, column=8, sticky="w", padx=(0, 4))
        self.baud_var = tk.StringVar(value="19200")
        self.baud_combo = ttk.Combobox(
            trans_frame, textvariable=self.baud_var, values=["9600", "19200", "38400", "115200"], width=8
        )
        self.baud_combo.grid(row=0, column=9, sticky="w", padx=(0, 12))

        self.push_btn = ttk.Button(trans_frame, text="PUSH SAMPLE", command=self._on_push_clicked)
        self.push_btn.grid(row=0, column=10, sticky="e")
        trans_frame.columnconfigure(10, weight=1)

        self._on_transport_changed()

        # 4. Live Wire Monitor Log
        log_frame = ttk.LabelFrame(right_frame, text="4. Live Wire Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, font=("Consolas", 9), bg="#11161d", fg="#d8dee9", state=tk.DISABLED
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)

        log_btns = ttk.Frame(log_frame)
        log_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(log_btns, text="Clear Log", command=self._clear_log).pack(side=tk.RIGHT)

    # ---------------- UI EVENT HANDLERS ----------------

    def _detect_com_ports(self) -> list[str]:
        if list_ports:
            ports = [p.device for p in list_ports.comports()]
            if ports:
                return ports
        return ["COM11", "COM10", "COM1", "COM3"]

    def _on_proto_changed(self, _event=None) -> None:
        proto = PROTOCOL_BY_LABEL[self.proto_var.get()]
        if proto == "4":
            self.carets_var.set(True)
            self.carets_chk.configure(state=tk.DISABLED)
        elif proto == "1":
            self.carets_chk.configure(state=tk.NORMAL)
        else:
            self.carets_var.set(False)
            self.carets_chk.configure(state=tk.DISABLED)

    def _on_transport_changed(self, _event=None) -> None:
        mode = self.transport_var.get()
        is_serial = mode == "Serial COM"
        self.host_entry.configure(state=tk.DISABLED if is_serial else tk.NORMAL)
        self.port_entry.configure(state=tk.DISABLED if is_serial else tk.NORMAL)
        self.com_combo.configure(state=tk.NORMAL if is_serial else tk.DISABLED)
        self.baud_combo.configure(state=tk.NORMAL if is_serial else tk.DISABLED)
        if mode == "TCP Server" and self.host_var.get() == "172.16.106.181":
            self.host_var.set("0.0.0.0")
        elif mode == "TCP Client" and self.host_var.get() == "0.0.0.0":
            self.host_var.set("172.16.106.181")

    def _clean_results_box(self) -> None:
        parsed = parse_results_block(self.results_text.get("1.0", tk.END), self.non_num_var.get())
        if not parsed:
            messagebox.showwarning("No Results", "No valid result rows found to format.")
            return

        formatted_lines = [
            f"{c}|{v}|{u}|{ov}" if ov else f"{c}|{v}|{u}"
            for c, v, u, ov in parsed
        ]
        self.results_text.delete("1.0", tk.END)
        self.results_text.insert("1.0", "\n".join(formatted_lines) + "\n")

    def _refresh_sample_list(self) -> None:
        self.sample_listbox.delete(0, tk.END)
        self.saved_files = sorted(SAMPLES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for fpath in self.saved_files:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                label = f"{data.get('source_uid', '?')} | {data.get('sample_id', '?')} ({len(data.get('results', []))})"
            except Exception:
                label = fpath.name
            self.sample_listbox.insert(tk.END, label)

    def _on_select_saved(self, _event=None) -> None:
        sel = self.sample_listbox.curselection()
        if not sel:
            return
        fpath = self.saved_files[sel[0]]
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Load Error", f"Could not read {fpath.name}: {exc}")
            return

        proto_code = data.get("protocol", "1")
        if proto_code in PROTOCOLS:
            self.proto_var.set(PROTOCOLS[proto_code])
        self.uid_var.set(data.get("source_uid", "INSTRUMENT"))
        self.sample_var.set(data.get("sample_id", ""))
        self.carets_var.set(bool(data.get("use_carets", False)))
        self._on_proto_changed()

        lines = [
            f"{r['code']}|{r['value']}|{r.get('unit', '')}|{r['barcode']}"
            if r.get("barcode")
            else f"{r['code']}|{r['value']}|{r.get('unit', '')}"
            for r in data.get("results", [])
        ]
        self.results_text.delete("1.0", tk.END)
        self.results_text.insert("1.0", "\n".join(lines) + "\n")
        self.log(f"[*] Loaded saved sample: {fpath.name}")

    def _delete_selected_sample(self) -> None:
        sel = self.sample_listbox.curselection()
        if not sel:
            return
        fpath = self.saved_files[sel[0]]
        if messagebox.askyesno("Delete Sample", f"Delete {fpath.name}?"):
            fpath.unlink(missing_ok=True)
            self._refresh_sample_list()

    def _save_current_sample(
        self, silent: bool = False, override_results: list[tuple[str, str, str, str]] | None = None
    ) -> None:
        proto = PROTOCOL_BY_LABEL[self.proto_var.get()]
        uid = self.uid_var.get().strip() or "INSTRUMENT"
        sample_id = self.sample_var.get().strip() or "NOSAMPLE"
        results = override_results or parse_results_block(self.results_text.get("1.0", tk.END), self.non_num_var.get())
        if not results:
            if not silent:
                messagebox.showwarning("Nothing to Save", "Enter at least one valid test result first.")
            return

        safe_uid = re.sub(r"[^A-Za-z0-9._-]+", "_", uid).strip("_") or "INST"
        safe_sid = re.sub(r"[^A-Za-z0-9._-]+", "_", sample_id).strip("_") or "SAMPLE"
        fpath = SAMPLES_DIR / f"{safe_uid}_{safe_sid}.json"

        payload = {
            "protocol": proto,
            "protocol_name": PROTOCOLS[proto],
            "source_uid": uid,
            "sample_id": sample_id,
            "use_carets": self.carets_var.get(),
            "saved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "results": [
                {"code": c, "value": v, "unit": u, **({"barcode": ov} if ov else {})}
                for c, v, u, ov in results
            ],
        }
        fpath.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._refresh_sample_list()
        self.log(f"[✓] Saved sample -> {fpath.name}")

    def _copy_hercules(self) -> None:
        proto = PROTOCOL_BY_LABEL[self.proto_var.get()]
        uid = self.uid_var.get().strip() or "INSTRUMENT"
        sample_id = self.sample_var.get().strip()
        results = parse_results_block(self.results_text.get("1.0", tk.END), self.non_num_var.get())
        if not results:
            messagebox.showwarning("No Results", "Enter at least one result first.")
            return

        batches = build_payload(proto, uid, sample_id, results, self.carets_var.get())
        lines: list[str] = []
        for barcode, batch_payload in batches:
            lines.append(f"// --- TUBE: {barcode} ---")
            if proto in ("1", "4"):
                for f in batch_payload:
                    txt = f[1:-4].decode("ascii")
                    cs = f[-4:-2].decode("ascii")
                    lines.append(f"$02{escape_hercules_macros(txt).replace(chr(13), '$0D')}$03{cs}$0D$0A")
            elif proto == "2":
                txt = batch_payload[1:-2].decode("ascii")
                lines.append(f"$0B{escape_hercules_macros(txt).replace(chr(13), '$0D')}$1C$0D")
            else:
                txt = batch_payload[1:-1].decode("ascii")
                lines.append(f"$02{escape_hercules_macros(txt)}$03")

        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self.log("[✓] Copied Hercules frames (HEX UNCHECKED) to clipboard.")

    # ---------------- TRANSMISSION WORKER ----------------

    def _on_push_clicked(self) -> None:
        proto = PROTOCOL_BY_LABEL[self.proto_var.get()]
        uid = self.uid_var.get().strip() or "INSTRUMENT"
        sample_id = self.sample_var.get().strip()
        raw_results = parse_results_block(self.results_text.get("1.0", tk.END), self.non_num_var.get())
        if not raw_results:
            messagebox.showwarning("No Results", "Enter at least one valid test result before pushing.")
            return

        if self.ask_tubes_var.get():
            dlg = TubeAssignmentDialog(self, sample_id, raw_results, self.tube_memory)
            self.wait_window(dlg)
            if dlg.confirmed_results is None:
                self.log("[*] Push cancelled in Tube Assignment dialog.")
                return
            results = dlg.confirmed_results
        else:
            results = [
                (c, v, u, ov or self.tube_memory.get(c.upper(), ""))
                for c, v, u, ov in raw_results
            ]

        self._save_current_sample(silent=True, override_results=results)

        groups = group_by_barcode(sample_id, results)
        self.log(
            "[*] Barcodes in batch: "
            + ", ".join(f"{bc} ({len(items)} tests)" for bc, items in groups)
        )

        # Apply Dedup-Bypass decimal nudge per tube group on repeat pushes
        key = f"{proto}:{uid}:{sample_id}"
        count = self.push_counter.get(key, 0) + 1
        self.push_counter[key] = count

        active_results = list(results)
        if self.dedup_var.get() and count > 1:
            nudged_barcodes: set[str] = set()
            for idx, (c, v, u, ov) in enumerate(active_results):
                bc = resolve_barcode(sample_id, ov)
                if bc in nudged_barcodes:
                    continue
                try:
                    num = float(v)
                    nudged = f"{num + (count - 1) * 0.001:.3f}".rstrip("0").rstrip(".")
                    active_results[idx] = (c, nudged, u, ov)
                    nudged_barcodes.add(bc)
                    self.log(f"[*] Dedup-Bypass ({bc} Push #{count}): {c} '{v}' -> '{nudged}'")
                except ValueError:
                    continue

        batches = build_payload(proto, uid, sample_id, active_results, self.carets_var.get())
        mode = self.transport_var.get()
        host = self.host_var.get().strip()
        port = int(self.port_var.get().strip() or "7010")
        com_port = self.com_var.get().strip()
        baud = int(self.baud_var.get().strip() or "9600")

        self.push_btn.configure(state=tk.DISABLED)
        threading.Thread(
            target=self._transmit_worker,
            args=(mode, host, port, com_port, baud, proto, batches),
            daemon=True,
        ).start()

    def _transmit_worker(
        self, mode: str, host: str, port: int, com_port: str, baud: int, proto: str, batches
    ) -> None:
        try:
            if mode == "TCP Client":
                if self.tcp_session_key != (host, port) or self.tcp_session is None:
                    if self.tcp_session:
                        self.tcp_session.close()
                    self.tcp_session = TcpClientSession(host, port)
                    self.tcp_session_key = (host, port)
                self.tcp_session.open(self.log)
                self._run_protocol(self.tcp_session.send, self.tcp_session.recv, self.tcp_session.reconnect, proto, batches)

            elif mode == "TCP Server":
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((host, port))
                srv.listen(1)
                srv.settimeout(20.0)
                self.log(f"[*] Listening on {host}:{port} (waiting up to 20s for driver)...")
                conn, addr = srv.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(3.0)
                self.log(f"[+] Driver connected from {addr[0]}:{addr[1]}")
                try:
                    def _recv(sz=1):
                        try:
                            return conn.recv(sz)
                        except socket.timeout:
                            return b""
                    self._run_protocol(conn.sendall, _recv, None, proto, batches)
                finally:
                    conn.close()
                    srv.close()

            elif mode == "Serial COM":
                if serial is None:
                    raise RuntimeError("pyserial not installed. Run: python -m pip install pyserial")
                self.log(f"[*] Opening {com_port} @ {baud} baud...")
                ser = serial.Serial(com_port, baudrate=baud, timeout=2.0, dsrdtr=False, rtscts=False)
                time.sleep(0.25)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                try:
                    def _send(d: bytes):
                        ser.write(d)
                        ser.flush()
                    self._run_protocol(_send, ser.read, None, proto, batches)
                finally:
                    ser.close()

        except Exception as exc:
            self.log(f"[!] Transmission Error: {exc}")
            if self.tcp_session:
                self.tcp_session.close()
        finally:
            self.after(0, lambda: self.push_btn.configure(state=tk.NORMAL))

    def _run_protocol(self, send_fn, recv_fn, reconnect_fn, proto: str, batches) -> None:
        total_tubes = len(batches)
        for tube_idx, (barcode, payload) in enumerate(batches, start=1):
            self.log(f"=== Transmitting Tube {tube_idx}/{total_tubes}: {barcode} ===")

            if proto in ("1", "4"):
                ok = False
                for attempt in range(1, 4):
                    self.log(f"[TX] 0x05 (ENQ) [Attempt {attempt}/3]")
                    try:
                        send_fn(ENQ)
                        resp = recv_fn(1)
                    except OSError:
                        resp = b""
                    if resp == ACK:
                        self.log("[RX] 0x06 (ACK) - Handshake OK!")
                        ok = True
                        break
                    self.log(f"[*] No ACK yet (got {resp!r}), retrying...")
                    if reconnect_fn:
                        reconnect_fn(self.log)
                    else:
                        time.sleep(1.0)

                if not ok:
                    self.log(f"[!] Handshake failed for {barcode} after 3 attempts.")
                    return

                for idx, frame in enumerate(payload, start=1):
                    preview = frame[1:-4].decode("ascii", errors="replace").replace("\r", " ")
                    self.log(f"[TX] Frame {idx}/{len(payload)}: {preview}")
                    send_fn(frame)
                    resp = recv_fn(1)
                    if resp == ACK:
                        self.log("[RX] 0x06 (ACK)")
                    elif resp == NAK:
                        self.log("[!] Driver returned 0x15 (NAK)! Aborting.")
                        send_fn(EOT)
                        return
                    else:
                        self.log(f"[!] Timeout or unexpected reply: {resp!r}")
                        return
                    time.sleep(0.05)

                self.log("[TX] 0x04 (EOT)")
                send_fn(EOT)
                self.log(f"[✓] Tube {barcode} transmitted cleanly!")

            elif proto == "2":
                self.log(f"[TX] Sending HL7 MLLP for {barcode} ({len(payload)} bytes)...")
                send_fn(payload)
                resp = recv_fn(1024)
                if resp:
                    self.log(f"[RX] MLLP Reply: {resp!r}")
                self.log(f"[✓] HL7 Message for {barcode} transmitted cleanly!")

            elif proto == "3":
                self.log(f"[TX] Sending Beckman AU frame for {barcode} ({len(payload)} bytes)...")
                send_fn(payload)
                resp = recv_fn(1)
                self.log(f"[RX] Reply: {resp!r}")
                self.log(f"[✓] Beckman AU frame for {barcode} transmitted cleanly!")

            if tube_idx < total_tubes:
                time.sleep(0.5)

        self.log(f"[✓] All {total_tubes} tube batch(es) completed!")

    # ---------------- LOGGING & CLEANUP ----------------

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _drain_log_queue(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, line + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)
        self.after(100, self._drain_log_queue)

    def _clear_log(self) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.tcp_session:
            self.tcp_session.close()
        self.destroy()


if __name__ == "__main__":
    app = LisSimulatorGUI()
    app.mainloop()

    