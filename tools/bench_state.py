"""tools/bench_state.py - what the machine was doing while a benchmark ran.

A run's speed depends on things the run does not control: how long the PC has been up, whether the GPU had boosted or was throttling, how hot it
was, whether Windows Update or Defender took a core in the middle of an arm.  docs/BENCH_NOISE.md found that the first minutes of a session ran 10%
to 35% slower and had to GUESS the cause from an arm's position in the session; this records it instead, once per run, into the run's own record.

    python tools/bench_state.py            print one snapshot (JSON)
    from bench_state import snapshot, Sampler

`snapshot()` is cheap (the GPU is read through NVML in-process, not by starting nvidia-smi: polling nvidia-smi every few seconds costs about
1 tok/s of decode, which is the kind of disturbance this exists to detect).  `Sampler` takes snapshots on a thread while an arm runs and
`summary()` says what happened across them.  Everything is best effort: a missing NVML, psutil or permission gives None for that field and never
an exception into the benchmark.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time

# Processes that take a core or the disk without being asked.  A run during which one of these was busy is not comparable to one during which none was.
BACKGROUND_WATCH = ("msmpeng", "mpdefendercoreservice", "tiworker", "trustedinstaller", "wuauclt", "usoclient", "musnotification", "searchindexer",
                    "onedrive", "backgroundtaskhost", "compattelrunner", "dism", "sdxhelper", "gamebar", "wmiprvse", "msiexec")
OWN = ("python", "strata", "nvidia", "claude", "code", "powershell", "pwsh", "cmd", "conhost", "git", "node", "msedge", "chrome")
BUSY_PERCENT = 15.0                  # of one core: a watched process above this was busy
THROTTLE_BITS = {0x1: "gpu idle", 0x2: "app clocks", 0x4: "sw power cap", 0x8: "hw slowdown", 0x20: "sw thermal", 0x40: "hw thermal",
                 0x80: "hw power brake"}

_NVML = None
_NVML_TRIED = False


def _nvml():
    """The loaded NVML library, or None.  Loaded once; the init call is made once."""
    global _NVML, _NVML_TRIED
    if _NVML_TRIED:
        return _NVML
    _NVML_TRIED = True
    names = (["nvml.dll", r"C:\Windows\System32\nvml.dll", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"] if os.name == "nt"
             else ["libnvidia-ml.so.1"])
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            if lib.nvmlInit_v2() == 0:
                _NVML = lib
                break
        except (OSError, AttributeError):
            continue
    return _NVML


def _uint(lib, fn, handle, *extra):
    value = ctypes.c_uint()
    return value.value if getattr(lib, fn)(handle, *extra, ctypes.byref(value)) == 0 else None


def gpu_state(index: int = 0) -> dict | None:
    """The first GPU's clocks, utilisation, temperature, power and memory, or None when NVML is not available."""
    lib = _nvml()
    if lib is None:
        return None
    try:
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
            return None

        class Mem(ctypes.Structure):
            _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]
        mem = Mem()
        have_mem = lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0
        class Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("mem", ctypes.c_uint)]
        util = Util()
        have_util = lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0
        throttle = ctypes.c_ulonglong()
        have_throttle = lib.nvmlDeviceGetCurrentClocksThrottleReasons(handle, ctypes.byref(throttle)) == 0
        power = _uint(lib, "nvmlDeviceGetPowerUsage", handle)
        return {"sm_mhz": _uint(lib, "nvmlDeviceGetClockInfo", handle, 1), "mem_mhz": _uint(lib, "nvmlDeviceGetClockInfo", handle, 2),
                "temp_c": _uint(lib, "nvmlDeviceGetTemperature", handle, 0), "power_w": round(power / 1000.0, 1) if power is not None else None,
                "util_gpu_pct": util.gpu if have_util else None, "util_mem_pct": util.mem if have_util else None,
                "mem_used_mib": round(mem.used / 2 ** 20) if have_mem else None,
                "pstate": _uint(lib, "nvmlDeviceGetPerformanceState", handle),
                "throttle": throttle.value if have_throttle else None}
    except Exception:  # noqa: BLE001 - a driver without one of the calls
        return None


def throttle_names(bits) -> list:
    return [name for bit, name in THROTTLE_BITS.items() if bits and bits & bit and bit != 0x1]    # "gpu idle" is the normal state, not a finding


def uptime_s() -> float | None:
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return round(ctypes.windll.kernel32.GetTickCount64() / 1000.0, 1)
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except Exception:  # noqa: BLE001
        return None


def free_ram_gib() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().available / 2 ** 30, 2)
    except Exception:  # noqa: BLE001
        pass
    if os.name != "nt":
        return None
    try:
        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
        m = MS()
        m.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return round(m.b / 2 ** 30, 2)
    except Exception:  # noqa: BLE001
        return None


def busy_background(window_s: float = 0.5) -> list | None:
    """Watched background processes using more than BUSY_PERCENT of a core over a short window, as [(name, percent)]; None without psutil."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        procs = []
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if any(w in name for w in BACKGROUND_WATCH) and not any(o in name for o in OWN):
                try:
                    p.cpu_percent(None)
                    procs.append((name, p))
                except psutil.Error:
                    pass
        if not procs:
            return []
        time.sleep(window_s)
        out = []
        for name, p in procs:
            try:
                pct = p.cpu_percent(None)
            except psutil.Error:
                continue
            if pct >= BUSY_PERCENT:
                out.append((name, round(pct)))
        return out
    except Exception:  # noqa: BLE001
        return None


def snapshot(background: bool = True) -> dict:
    """Everything above at one moment.  Fields that cannot be read are None."""
    snap = {"time": round(time.time(), 1), "uptime_s": uptime_s(), "free_ram_gib": free_ram_gib(), "gpu": gpu_state()}
    if snap["gpu"] is not None:
        snap["gpu"]["throttle_reasons"] = throttle_names(snap["gpu"].get("throttle"))
    if background:
        snap["background_busy"] = busy_background()
    return snap


def summarize_samples(samples: list, interval: float = 10.0) -> dict:
    """What a run's samples say: GPU clock range and mean utilisation, the hottest and most power-hungry moments, throttling seen, the least free
    RAM, and every watched background process that was busy at any sample.  A run whose samples sit in a different clock band from its
    comparison arm is not the same machine state, whatever the speeds say."""
    def series(path):
        out = []
        for s in samples:
            v = s
            for k in path:
                v = v.get(k) if isinstance(v, dict) else None
            if isinstance(v, (int, float)):
                out.append(v)
        return out
    sm, temp, power, ram = series(("gpu", "sm_mhz")), series(("gpu", "temp_c")), series(("gpu", "power_w")), series(("free_ram_gib",))
    util = series(("gpu", "util_gpu_pct"))
    seen_throttle, seen_busy = set(), {}
    for s in samples:
        seen_throttle.update(((s.get("gpu") or {}).get("throttle_reasons")) or [])
        for name, pct in s.get("background_busy") or []:
            seen_busy[name] = max(seen_busy.get(name, 0), pct)
    return {"samples": len(samples), "interval_s": interval,
            "sm_mhz": {"min": min(sm), "max": max(sm), "mean": round(sum(sm) / len(sm))} if sm else None,
            # The utilisation is the mean, not the max: it is the field that says whether the arm ran while the GPU was
            # busy with something else, and one sample of it (docs/BENCH_NOISE.md, "What to do about it" 4) is enough to
            # tell the cold region apart from a result.  A mean over the samples says the same thing and does not hide a
            # run that was busy throughout; the max would let a single 100% sample stand for an idle arm.
            "util_gpu_pct_mean": round(sum(util) / len(util), 1) if util else None,
            "temp_c_max": max(temp) if temp else None, "power_w_max": max(power) if power else None,
            "free_ram_gib_min": min(ram) if ram else None,
            "throttle_seen": sorted(seen_throttle), "background_busy": seen_busy}


class Sampler:
    """Snapshots on a thread every `interval` seconds until stop(); summary() reads them.  Start it when an arm starts."""
    def __init__(self, interval: float = 10.0, background: bool = True):
        self.interval, self.background, self.samples = interval, background, []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="bench-state", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.samples.append(snapshot(self.background))
            except Exception:  # noqa: BLE001 - never into the benchmark
                pass
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self.summary()

    def summary(self) -> dict:
        return summarize_samples(list(self.samples), self.interval)


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=1))
    sys.exit(0)
