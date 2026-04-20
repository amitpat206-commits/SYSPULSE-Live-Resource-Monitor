"""
SysPulse — Per-Application Resource Monitor
Tracks CPU, RAM, and GPU usage per process on Windows.
Requires: psutil, rich, pynvml (optional, for NVIDIA GPUs)
"""

import sys, os, time, subprocess, threading, signal
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ─── Auto-install dependencies ───────────────────────────────────────────────
def _ensure(pkg, import_as=None):
    name = import_as or pkg
    try:
        __import__(name)
    except ImportError:
        print(f"[setup] Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

_ensure("psutil")
_ensure("rich")
_ensure("pynvml")

# ─── Imports after install ────────────────────────────────────────────────────
import psutil
from rich.console import Console
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich.progress import BarColumn, Progress, TextColumn
from rich import box
from rich.columns import Columns
from rich.align import Align
from rich.style import Style

try:
    import pynvml
    pynvml.nvmlInit()
    GPU_AVAILABLE = True
    GPU_COUNT = pynvml.nvmlDeviceGetCount()
except Exception:
    GPU_AVAILABLE = False
    GPU_COUNT = 0

# ─── Data structures ──────────────────────────────────────────────────────────
@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_pct: float = 0.0
    ram_mb: float = 0.0
    gpu_mem_mb: float = 0.0
    gpu_pct: float = 0.0
    status: str = "running"
    username: str = ""

@dataclass
class GPUInfo:
    name: str
    util_pct: float
    mem_used_mb: float
    mem_total_mb: float
    temp_c: float
    process_gpu_mem: dict = field(default_factory=dict)  # pid → MB

# ─── GPU helpers ─────────────────────────────────────────────────────────────
def get_gpu_info() -> list[GPUInfo]:
    if not GPU_AVAILABLE:
        return []
    gpus = []
    for i in range(GPU_COUNT):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = 0

            proc_mem: dict[int, float] = {}
            for proc_type in [
                pynvml.nvmlDeviceGetComputeRunningProcesses,
                pynvml.nvmlDeviceGetGraphicsRunningProcesses,
            ]:
                try:
                    for p in proc_type(handle):
                        pid = p.pid
                        mb = p.usedGpuMemory / (1024 ** 2) if p.usedGpuMemory else 0
                        proc_mem[pid] = proc_mem.get(pid, 0) + mb
                except Exception:
                    pass

            gpus.append(GPUInfo(
                name=name,
                util_pct=util.gpu,
                mem_used_mb=mem.used / (1024 ** 2),
                mem_total_mb=mem.total / (1024 ** 2),
                temp_c=temp,
                process_gpu_mem=proc_mem,
            ))
        except Exception:
            pass
    return gpus

# ─── Process collection ───────────────────────────────────────────────────────
class ProcessCollector:
    def __init__(self):
        self._procs: dict[int, psutil.Process] = {}
        self._lock = threading.Lock()
        self._snapshot: list[ProcessInfo] = []

    def _init_procs(self):
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc.cpu_percent(interval=None)
                self._procs[proc.pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def collect(self) -> list[ProcessInfo]:
        gpu_infos = get_gpu_info()
        pid_gpu_mem: dict[int, float] = defaultdict(float)
        total_gpu_mem: float = sum(g.mem_total_mb for g in gpu_infos) or 1

        for g in gpu_infos:
            for pid, mb in g.process_gpu_mem.items():
                pid_gpu_mem[pid] += mb

        results: list[ProcessInfo] = []
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info", "status", "username"]
        ):
            try:
                info = proc.info
                pid = info["pid"]
                name = info["name"] or "Unknown"
                cpu = info.get("cpu_percent") or 0.0
                mem_info = info.get("memory_info")
                ram = mem_info.rss / (1024 ** 2) if mem_info else 0.0
                status = info.get("status", "")
                username = info.get("username") or ""
                if username and "\\" in username:
                    username = username.split("\\")[-1]

                gm = pid_gpu_mem.get(pid, 0.0)
                gpu_pct = (gm / total_gpu_mem * 100) if total_gpu_mem else 0.0

                if cpu > 0 or ram > 1 or gm > 0:
                    results.append(ProcessInfo(
                        pid=pid,
                        name=name,
                        cpu_pct=cpu,
                        ram_mb=ram,
                        gpu_mem_mb=gm,
                        gpu_pct=gpu_pct,
                        status=status,
                        username=username,
                    ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        results.sort(key=lambda p: p.cpu_pct + p.ram_mb / 100, reverse=True)
        return results

# ─── UI builders ──────────────────────────────────────────────────────────────
THEME = {
    "bg":         "#0a0e17",
    "accent":     "#00f5d4",
    "accent2":    "#f72585",
    "warn":       "#ffd166",
    "ok":         "#06d6a0",
    "text":       "#cdd6f4",
    "dim":        "#565f89",
    "cpu_bar":    "#f72585",
    "ram_bar":    "#00b4d8",
    "gpu_bar":    "#7b2d8b",
    "header_bg":  "#1e2030",
}

def _bar(pct: float, width: int = 10) -> Text:
    filled = int(pct / 100 * width)
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    color = (
        THEME["ok"] if pct < 50
        else THEME["warn"] if pct < 80
        else THEME["accent2"]
    )
    t = Text()
    t.append(bar, style=f"bold {color}")
    t.append(f" {pct:5.1f}%", style=THEME["text"])
    return t

def _ram_bar(mb: float, total_mb: float, width: int = 8) -> Text:
    pct = (mb / total_mb * 100) if total_mb else 0
    filled = int(pct / 100 * width)
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    color = (
        THEME["ok"] if pct < 50
        else THEME["warn"] if pct < 80
        else THEME["accent2"]
    )
    t = Text()
    t.append(bar, style=f"bold {color}")
    t.append(f" {mb:7.1f}M", style=THEME["text"])
    return t

def build_header(cpu_pct, ram_pct, ram_used, ram_total, gpu_infos) -> Panel:
    lines = []

    # CPU
    cpu_fill = int(cpu_pct / 100 * 30)
    cpu_bar_str = "▰" * cpu_fill + "▱" * (30 - cpu_fill)
    cpu_color = THEME["ok"] if cpu_pct < 50 else THEME["warn"] if cpu_pct < 80 else THEME["accent2"]
    t = Text()
    t.append("  CPU  ", style=f"bold {THEME['accent']}")
    t.append(cpu_bar_str, style=f"bold {cpu_color}")
    t.append(f"  {cpu_pct:5.1f}%", style=f"bold {THEME['text']}")
    lines.append(t)

    # RAM
    ram_fill = int(ram_pct / 100 * 30)
    ram_bar_str = "▰" * ram_fill + "▱" * (30 - ram_fill)
    ram_color = THEME["ok"] if ram_pct < 50 else THEME["warn"] if ram_pct < 80 else THEME["accent2"]
    t2 = Text()
    t2.append("  RAM  ", style=f"bold {THEME['ram_bar']}")
    t2.append(ram_bar_str, style=f"bold {ram_color}")
    t2.append(f"  {ram_pct:5.1f}%  {ram_used:.1f} / {ram_total:.1f} GB", style=f"bold {THEME['text']}")
    lines.append(t2)

    # GPU(s)
    if gpu_infos:
        for g in gpu_infos:
            gfill = int(g.util_pct / 100 * 30)
            g_bar_str = "▰" * gfill + "▱" * (30 - gfill)
            g_color = THEME["ok"] if g.util_pct < 50 else THEME["warn"] if g.util_pct < 80 else THEME["accent2"]
            t3 = Text()
            t3.append("  GPU  ", style=f"bold {THEME['gpu_bar']}")
            t3.append(g_bar_str, style=f"bold {g_color}")
            t3.append(
                f"  {g.util_pct:5.1f}%  {g.mem_used_mb:.0f} / {g.mem_total_mb:.0f} MB  🌡 {g.temp_c}°C  {g.name}",
                style=f"bold {THEME['text']}"
            )
            lines.append(t3)
    else:
        t3 = Text()
        t3.append("  GPU  ", style=f"bold {THEME['gpu_bar']}")
        t3.append("No NVIDIA GPU detected / pynvml unavailable", style=THEME["dim"])
        lines.append(t3)

    content = Text("\n").join(lines)
    return Panel(
        content,
        title=Text("⚡ SYSPULSE — Live Resource Monitor", style=f"bold {THEME['accent']}"),
        border_style=THEME["accent"],
        padding=(0, 1),
    )

def build_table(procs: list[ProcessInfo], ram_total_mb: float, show_rows: int = 40) -> Table:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        border_style=THEME["dim"],
        header_style=f"bold {THEME['accent']}",
        show_edge=False,
        expand=True,
        padding=(0, 1),
    )

    tbl.add_column("PID",      style=THEME["dim"],   width=7,  justify="right")
    tbl.add_column("PROCESS",  style=f"bold {THEME['text']}", min_width=24, max_width=36)
    tbl.add_column("USER",     style=THEME["dim"],   width=12)
    tbl.add_column("CPU %",    width=18)
    tbl.add_column("RAM",      width=18)
    tbl.add_column("GPU MEM",  width=18)
    tbl.add_column("STATUS",   width=10)

    for p in procs[:show_rows]:
        # CPU bar
        cpu_fill = int(p.cpu_pct / 100 * 8)
        cpu_bar_c = THEME["ok"] if p.cpu_pct < 50 else THEME["warn"] if p.cpu_pct < 80 else THEME["accent2"]
        cpu_t = Text()
        cpu_t.append("█" * cpu_fill + "░" * (8 - cpu_fill), style=f"bold {cpu_bar_c}")
        cpu_t.append(f" {p.cpu_pct:5.1f}%", style=THEME["text"])

        # RAM bar
        ram_pct = (p.ram_mb / ram_total_mb * 100) if ram_total_mb else 0
        ram_fill = int(ram_pct / 100 * 8)
        ram_c = THEME["ok"] if ram_pct < 50 else THEME["warn"] if ram_pct < 80 else THEME["accent2"]
        ram_t = Text()
        ram_t.append("█" * ram_fill + "░" * (8 - ram_fill), style=f"bold {ram_c}")
        if p.ram_mb >= 1024:
            ram_t.append(f" {p.ram_mb/1024:5.1f}G", style=THEME["text"])
        else:
            ram_t.append(f" {p.ram_mb:5.0f}M", style=THEME["text"])

        # GPU bar
        if p.gpu_mem_mb > 0:
            gm_fill = min(8, int(p.gpu_pct / 100 * 8))
            gm_t = Text()
            gm_t.append("█" * gm_fill + "░" * (8 - gm_fill), style=f"bold {THEME['gpu_bar']}")
            gm_t.append(f" {p.gpu_mem_mb:5.0f}M", style=THEME["text"])
        else:
            gm_t = Text("─", style=THEME["dim"])

        # Status
        status_color = {
            "running": THEME["ok"],
            "sleeping": THEME["dim"],
            "idle": THEME["dim"],
            "stopped": THEME["warn"],
            "zombie": THEME["accent2"],
            "disk-sleep": THEME["warn"],
        }.get(p.status, THEME["dim"])

        status_t = Text(p.status[:8], style=status_color)

        tbl.add_row(
            str(p.pid),
            p.name[:36],
            (p.username or "─")[:12],
            cpu_t,
            ram_t,
            gm_t,
            status_t,
        )

    return tbl

# ─── Main loop ────────────────────────────────────────────────────────────────
def main():
    console = Console()
    collector = ProcessCollector()
    REFRESH = 1.5  # seconds

    vm = psutil.virtual_memory()
    ram_total_mb = vm.total / (1024 ** 2)
    ram_total_gb = vm.total / (1024 ** 3)

    console.clear()

    with Live(console=console, refresh_per_second=int(1 / REFRESH * 2), screen=True) as live:
        while True:
            try:
                # System-level stats
                cpu_pct  = psutil.cpu_percent(interval=None)
                vm       = psutil.virtual_memory()
                ram_used = vm.used / (1024 ** 3)
                ram_pct  = vm.percent

                gpu_infos = get_gpu_info()
                procs     = collector.collect()

                # How many rows fit?
                term_h  = console.size.height
                rows    = max(5, term_h - 14)

                header  = build_header(cpu_pct, ram_pct, ram_used, ram_total_gb, gpu_infos)
                table   = build_table(procs, ram_total_mb, show_rows=rows)

                footer = Text(
                    f"  {len(procs)} active processes  ·  refresh {REFRESH}s  ·  sorted by CPU+RAM  ·  [Ctrl+C] quit",
                    style=THEME["dim"]
                )

                layout = Layout()
                layout.split_column(
                    Layout(header,  name="header", size=4 + len(gpu_infos)),
                    Layout(table,   name="table",  ratio=1),
                    Layout(footer,  name="footer", size=1),
                )

                live.update(layout)
                time.sleep(REFRESH)

            except KeyboardInterrupt:
                break
            except Exception as e:
                time.sleep(REFRESH)

    console.print(f"\n[{THEME['accent']}]SysPulse[/] session ended.\n")

if __name__ == "__main__":
    main()
