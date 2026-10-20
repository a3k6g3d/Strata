"""The rank-margin gate's own logic, on a fabricated trace and a fabricated profile.

Offline: no server, no GPU, no real trace file — both are written to a temp dir in the formats the real
loaders read, so the test pins the mechanism and not any particular machine's numbers.

What the gate is (RELATED_WORK, "the lever worth building"): `warm()` is unconditional today, so every
predicted expert no tier holds spends I/O.  The gate says: only the first `k - gate` ranks of a row may
*earn* residency.  It keys on rank because the serve-path traces write the weight column as zeros
(generate.cpp:1023) — which is also why this test fabricates ids in rank order and never a score.

Two things the fixture is forced to have, and both are properties of the real harness, not conveniences:

* `adapt_round` only ever swaps a candidate *against an existing resident* (it needs a victim), so a run
  that starts with nothing resident can never admit anything.  A real run starts from the profile; so does this.
* If every rank earned the same usage the gate could not change the winner, so the fabricated trace makes
  the *tail* rank the most frequent earner — the exact situation the gate is for.  Row A is the plain
  rank-ordered row; row B repeats the tail expert, which is what a real row does when one expert is routed
  for several tokens at once.  And the counts must not tie at the final round: `adapt_round` breaks ties by
  expert id, so a tie would test the tie-breaker and not the gate.  Nine windows, five A and four B, which
  leaves each gate a strict winner of its own.

    python -m pytest tools/test_replay_cache_policy.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import replay_cache_policy as R  # noqa: E402

K = 3
HEAD, MID, TAIL = 10, 11, 12      # the three ranks of every row, best rank first
SEED = 20                         # the profile's expert: resident, never routed, so it is the victim
ROW_A = [HEAD, MID, TAIL]         # the plain rank-ordered row
ROW_B = [MID, TAIL, TAIL]         # the tail expert appears twice, so it out-earns the head


def write_trace(path, n_windows=9):
    """One token per layer per window; rows alternate A/B by layer so the tail rank earns the most."""
    with open(path, "wb") as f:
        for wi in range(n_windows):
            for layer in range(R.NL):
                row = ROW_A if wi % 2 == 0 else ROW_B
                f.write(np.array([layer, K], dtype=np.int32).tobytes())
                f.write(np.array(row, dtype=np.int32).tobytes())
                f.write(np.zeros(K, dtype=np.float32).tobytes())   # zeros, exactly as the engine writes them
    return path


def write_profile(path):
    with open(path, "wb") as f:
        f.write(b"STRP")
        f.write(np.array([1, R.NL, 512, R.NL, R.NL], dtype=np.uint32).tobytes())
        for layer in range(R.NL):
            f.write(np.array([layer, SEED], dtype=np.uint16).tobytes())
    return path


def sweep(trace, profile, gates):
    windows, k = R.load_trace(trace)
    assert k == K and windows, "the fabricated trace must load through the real loader"
    init = R.load_profile(profile, R.NL)
    pol = lambda g: R.Policy(every=1, decay=1.0, swaps=10**6, margin=0.0, mincand=1.0, gate=g)
    return [R.simulate(windows, init, pol(g)) for g in gates]


# Usage each rank can earn over the whole fixture, per layer, when every rank may earn:
# the tail wins (1.5 rows' worth against the head's 0.5), so ungated the tail takes the slot.
# Gate 1 removes the tail's third position and the head's, leaving the middle rank the winner;
# gate 2 leaves only rank 1, where the head wins.  Three gates, three different winners.
WINNERS = {0: TAIL, 1: MID, 2: HEAD}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trace = write_trace(Path(self.tmp.name) / "t.bin")
        self.profile = write_profile(Path(self.tmp.name) / "p.bin")
        self.addCleanup(self.tmp.cleanup)

    def test_the_gate_changes_the_winner(self):
        # A real narrowing has to come out as a real difference, or the sweep measures nothing.
        shares = [r["share"] for r in sweep(self.trace, self.profile, (0, 1, 2))]
        self.assertGreater(shares[0], shares[1])
        self.assertGreater(shares[1], shares[2])

    def test_which_rank_wins_is_which_rank_the_gate_leaves_earning(self):
        # The mechanism, stated as an end state: after the last adaptation round the one resident slot
        # per layer holds the rank that earned the most among those still allowed to earn.
        windows, _ = R.load_trace(self.trace)
        init = R.load_profile(self.profile, R.NL)
        for gate, winner in WINNERS.items():
            pol = R.Policy(every=1, decay=1.0, swaps=10**6, margin=0.0, mincand=1.0, gate=gate)
            res = np.zeros((R.NL, 512), bool)
            res[init[:, 0], init[:, 1]] = True
            usage = np.zeros((R.NL, 512))
            for w in windows:
                for l, ids in w:
                    earn = ids[:, :ids.shape[1] - pol.gate] if pol.gate else ids
                    np.add.at(usage[l], earn.ravel(), 1.0)
                R.adapt_round(res, usage, pol)
            resident = np.flatnonzero(res[0])
            self.assertEqual(list(resident), [winner], "gate %d" % gate)

    def test_the_trade_is_the_trade(self):
        # The point of the gate is the exchange rate: hit share given up for fewer distinct reads.
        # Direction only here — on this fixture the count of distinct misses is the same, because one
        # slot holds one expert whichever rank won.  The sweep's real traces are where it moves.
        r0, r2 = sweep(self.trace, self.profile, (0, 2))
        self.assertLess(r2["share"], r0["share"])
        self.assertGreaterEqual(r2["miss_dist"], r0["miss_dist"])

    def test_hits_are_counted_over_the_whole_row(self):
        # The gate narrows what may earn residency; it does not change what the layer computes.
        # So the entry count must not move when the gate does — only which of them are hits.
        r0, r2 = sweep(self.trace, self.profile, (0, 2))
        self.assertEqual(r0["entries"], r2["entries"])


if __name__ == "__main__":
    unittest.main()
