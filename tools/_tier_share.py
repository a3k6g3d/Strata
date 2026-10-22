"""The tier-split batch, all ten launches, one table.

Runs tools/read_window_profile.py on each launch folder's engine-log.txt - the path the pre-registered rule
names - and prints the lines the doc quotes: waitB's share, and the two tiers' counts and clocks.
Rows are round-pairs: baseline then arm, so the two arms of a pair sit next to each other.

    python tools/_tier_share.py bench/results/ab-tier-split-rerun
"""
import os
import subprocess
import sys

PAIRS = [
    ("as-configured", "pcie-frac-0-35"),
    ("as-configured-2", "pcie-frac-0-35-2"),
    ("as-configured-3", "pcie-frac-0-35-3"),
    ("as-configured-4", "pcie-frac-0-35-4"),
    ("as-configured-5", "pcie-frac-0-35-5"),
]
KEEP = ("waitB", "tier blobs", "tier ms")


def folder(root, label):
    for d in sorted(os.listdir(root)):
        if d.endswith("-" + label) and os.path.isdir(os.path.join(root, d)):
            return os.path.join(root, d, "engine-log.txt")
    raise SystemExit(f"no launch folder ends with -{label}")


def main(argv=None):
    root = (argv or ["bench/results/ab-tier-split-rerun"])[0]
    for base, arm in PAIRS:
        for label in (base, arm):
            out = subprocess.run(
                [sys.executable, "tools/read_window_profile.py", folder(root, label)],
                capture_output=True, text=True,
            ).stdout
            print(f"{label:20s}")
            for line in out.splitlines():
                if any(k in line for k in KEEP):
                    print("   ", line.strip())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
