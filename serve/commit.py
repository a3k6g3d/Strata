"""Windows commit headroom: how much more memory the PC can promise before an allocation fails.

The commit limit is RAM plus the pagefile(s).  When commit gets close to it Windows refuses allocations (an application
dies with an out-of-memory error, or the PC grinds), well before RAM itself looks full.  `level` is the one word a front end
needs: "ok", "low" (under LOW_FREE_GIB / LOW_FREE_PCT) or "critical" (under CRITICAL_FREE_GIB).  Without Windows (or when the
counters cannot be read) `read()` returns None and nothing is warned about.  The fix this points to is a larger pagefile on
the fast drive (System Properties > Advanced > Performance > Virtual memory), not anything in Strata."""
import ctypes
import sys

LOW_FREE_GIB = 8.0        # under this much (or LOW_FREE_PCT of the limit), ask the user to look
LOW_FREE_PCT = 10.0
CRITICAL_FREE_GIB = 3.0   # under this, the next big allocation can fail


class _PerfInfo(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t), ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t), ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", ctypes.c_ulong),
                ("ProcessCount", ctypes.c_ulong), ("ThreadCount", ctypes.c_ulong)]


def classify(used_gib: float, limit_gib: float) -> str:
    free = limit_gib - used_gib
    if free < CRITICAL_FREE_GIB:
        return "critical"
    if free < LOW_FREE_GIB or free < limit_gib * LOW_FREE_PCT / 100:
        return "low"
    return "ok"


def read():
    """{"used_gib", "limit_gib", "free_gib", "peak_gib", "pagefile_gib", "level"} or None."""
    if sys.platform != "win32":
        return None
    try:
        info = _PerfInfo()
        info.cb = ctypes.sizeof(_PerfInfo)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        gib = lambda pages: pages * info.PageSize / 2 ** 30
        used, limit = gib(info.CommitTotal), gib(info.CommitLimit)
        if limit <= 0:
            return None
        return {"used_gib": round(used, 1), "limit_gib": round(limit, 1), "free_gib": round(limit - used, 1),
                "peak_gib": round(gib(info.CommitPeak), 1), "pagefile_gib": round(max(0.0, limit - gib(info.PhysicalTotal)), 1),
                "level": classify(used, limit)}
    except Exception:  # noqa: BLE001 - a missing DLL or a changed ABI: no reading, no warning
        return None


def advice(c) -> str:
    """The sentence for the log and the toast; "" when there is nothing to say."""
    if not c or c["level"] == "ok":
        return ""
    head = (f"Memory commit is {c['used_gib']:.0f} of {c['limit_gib']:.0f} GiB ({c['free_gib']:.0f} GiB left) - "
            f"{'allocations are about to fail' if c['level'] == 'critical' else 'running low'}.")
    return head + (" The pagefile is small or off: set it to 64 GB on the fast drive (Advanced system settings > Performance > "
                   "Virtual memory), or lower --resident-budget-gib." if c["pagefile_gib"] < 16 else
                   " Close other programs, or lower --resident-budget-gib.")
