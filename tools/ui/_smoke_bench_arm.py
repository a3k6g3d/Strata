"""Throwaway check of the new A/B pieces of tools/strata_bench.py against the running server."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import strata_bench as B  # noqa: E402

BASE = "http://127.0.0.1:8080"
pre = B.collect_machine(BASE, "", None)
print("totals before:", pre["totals"].get("requests"), "kept:", pre["requests_kept"])
print("engine: slots", pre["engine_info"].get("expert_slots"), "spec", pre["engine_info"].get("spec"))

out = {"chat": {"decode_tps": {"median": 12.4, "min": 11.1, "max": 13.0, "n": 3}, "ttft_s": {"median": 0.9, "min": 0.8, "max": 1.0, "n": 3}},
       "prompts": {"4K": {"prompt_tps": {"median": 350.0, "min": 340, "max": 360, "n": 3}, "ttft_s": {"median": 12.0, "min": 11, "max": 13, "n": 3}}},
       "agent": {}, "clients": {}, "skipped": {}, "failures": [],
       "phases": [{"name": "short chat", "from_s": time.time() - 900, "to_s": time.time()},
                  {"name": "4K prompt", "from_s": time.time() - 1e6, "to_s": time.time() - 999999}]}
eng = B.engine_attribution(BASE, "", out, pre["totals"])
print(json.dumps(eng, indent=1)[:1200])
out["engine"] = eng

res = {"schema": B.SCHEMA, "tool": "x", "date": "2026-10-12",
       "params": {"runs": 1, "think": False, "clients": 0, "long": False, "started_server": False,
                  "tests": ["chat"], "arm": "expert-cache off", "note": "Does the expert cache carry the speed, or the drafts?",
                  "engine_arg": ["--expert-cache", "0"], "command": "x"},
       "elapsed_s": 10, "machine": pre, "results": out}
txt = B.render_readme(res)
print("---- README head ----")
print("\n".join(txt.splitlines()[:12]))
print("---- cache section ----")
i = txt.find("expert cache and the drafts")
print(txt[i - 6:i + 700] if i > 0 else "SECTION MISSING")
print("---- compare with itself ----")
print(B.compare_table(res, res))
print("---- short table ----")
print(B.short_table(res))
print("---- dir name ----", B.results_dir_name(pre, "2026-10-12", "expert-cache off"))
