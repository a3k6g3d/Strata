import json, os, subprocess, sys

BASE = r"J:\Strata-work\Strata-0140"
SEG = os.path.join(BASE, "bench", "results", "ab-tier-split-rerun", "_segments")
lines = open(r"J:\Strata-work\strata.log", encoding="utf-8", errors="replace").read().splitlines()
rows = [json.loads(l) for l in open(os.path.join(BASE, "bench/results/ab-tier-split-rerun/runs.jsonl"), encoding="utf-8") if l.strip()]
starts = [i for i, l in enumerate(lines) if l.startswith("FileExpertSource: mapped pinned cache complement ready")]
first = 293

for k, r in enumerate(rows):
    end = starts[first + k + 1] if first + k + 1 < len(starts) else len(lines)
    seg = lines[starts[first + k]:end]
    lab = r["folder"].split("-i-")[1]
    path = os.path.join(SEG, lab + ".log")
    open(path, "w", encoding="utf-8").write("\n".join(seg) + "\n")
    nt = sum(1 for l in seg if l.startswith("strata decode tier split"))
    nd = sum(1 for l in seg if l.startswith("strata decode timing:"))
    print(f"{lab:22s} {len(seg):5d} lines, {nt:3d} tier, {nd:3d} timing")

print()
for k, r in enumerate(rows):
    lab = r["folder"].split("-i-")[1]
    print("=" * 100)
    subprocess.run([sys.executable, os.path.join(BASE, "tools", "read_window_profile.py"),
                    os.path.join(SEG, lab + ".log")], cwd=BASE)
