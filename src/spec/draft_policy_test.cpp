// src/spec/draft_policy_test.cpp - DraftPolicy: when does a lookup window beat the MTP's?
//
// Simulated rounds with the costs measured on the RTX 5070 (window-cost: ~10 ms more per token) check that
//   1. with no lookup proposal the MTP window is kept;
//   2. lookup drafts that are mostly rejected stop being taken (their bucket's rate falls);
//   3. lookup drafts that are always accepted are taken, and the window grows with them;
//   4. match-length buckets learn separately (short matches failing does not stop long ones);
//   5. the policy never proposes a window beyond its cap.
// and --spec-adaptive's MTP window (choose_mtp), over simulated rounds with a draft head whose probabilities are
// not calibrated (accepted at 0.15 + 0.8 p) and two cost curves (missed experts on the CPU: +10.5 ms per token;
// every expert in VRAM: +1.2 ms):
//   6. c(p) learns the true acceptance per probability bin;
//   7. committed tokens per ms are at least --spec-min-p 0.5's on both curves, and higher where tokens are cheap.
#include "strata/spec/draft_policy.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

using strata::spec::DraftPolicy;

namespace {
int g_fail = 0;
void check(bool ok, const char* what) {
    std::printf("  %-66s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++g_fail;
}
double cost(int t) { return 19.0 + 10.5 * (t - 1); }   // ms per round, measured shape

double true_accept(float p) { return 0.15 + 0.8 * p; }  // a head that is under-confident at low p, over at high

// One simulated decode: `rounds` windows of up to max_t, drafts' probabilities from a mix (confident, middling,
// unsure), a draft step costing 0.5 ms. adaptive: choose_mtp with the chain floor; else --spec-min-p `min_p`.
// Returns committed tokens per ms.
double simulate(bool adaptive, float min_p, double (*cost_of)(int), int max_t, int rounds, DraftPolicy* out = nullptr) {
    std::mt19937 rng(7);
    std::uniform_real_distribution<float> u(0.f, 1.f);
    auto draw_p = [&]() {
        const float r = u(rng);
        return r < 0.55f ? 0.8f + 0.2f * u(rng) : r < 0.85f ? 0.3f + 0.5f * u(rng) : 0.3f * u(rng);
    };
    DraftPolicy pol(max_t);
    double ms = 0.0, tokens = 0.0;
    std::vector<float> prob((size_t) max_t, 0.f);
    const float floor = adaptive ? DraftPolicy::kChainFloor : min_p;
    float table[DraftPolicy::kPBins];
    for (int r = 0; r < rounds; ++r) {
        // the chain: drafts while the last one is at least `floor` likely (that one included), as MtpDrafter::draft
        int n = 0;
        std::fill(prob.begin(), prob.end(), 0.f);
        // adaptive: also while every draft so far is likely enough to be accepted (MtpDrafter::draft's min_reach)
        float pj = 1.f, reach = 1.f;
        pol.accept_table(table);
        while (n < max_t - 1 && pj >= floor && (!adaptive || reach >= DraftPolicy::kChainReach)) {
            pj = std::max(draw_p(), 1e-3f);
            prob[(size_t) n++] = pj;
            reach *= table[std::clamp((int) (pj * DraftPolicy::kPBins), 0, DraftPolicy::kPBins - 1)];
        }
        int T = 1;
        if (adaptive) T = pol.choose_mtp(prob.data(), n, max_t);
        else while (T < max_t && prob[(size_t) T - 1] >= min_p) ++T;
        int a = 0;
        while (a < T - 1 && u(rng) < true_accept(prob[(size_t) a])) ++a;
        const double round_ms = cost_of(T) + 0.5 * n;
        if (adaptive) pol.observe_drafts(prob.data(), T, a);
        pol.observe(false, T, a, 0, round_ms);
        ms += round_ms;
        tokens += a + 1;
    }
    if (out) *out = pol;
    return tokens / ms;
}
double cost_cpu(int t) { return 19.0 + 10.5 * (t - 1); }
double cost_vram(int t) { return 12.0 + 1.2 * (t - 1); }
}  // namespace

int main() {
    std::printf("draft_policy_test\n");
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 2, 0, cost(4));   // MTP windows of 4: 3 tokens each
        const DraftPolicy::Pick k = p.choose(4, 0, 0);
        check(!k.lookup && k.t == 4, "no proposal: the MTP window");
    }
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 2, 0, cost(4));
        for (int t = 2; t <= 6; ++t) p.observe(false, t, 0, 0, cost(t));
        for (int i = 0; i < 40; ++i) p.observe(true, 6, 0, 4, cost(6));      // short matches, all rejected
        check(p.lookup_rate(4) < 0.15, "rejected short-match drafts: their rate falls below 0.15");
        check(!p.choose(4, 5, 4).lookup, "rejected short-match drafts: no longer taken");
        for (int i = 0; i < 40; ++i) p.observe(true, 6, 5, 30, cost(6));     // long matches, all accepted
        check(p.lookup_rate(30) > 0.9, "accepted long-match drafts: their rate rises above 0.9");
        const DraftPolicy::Pick k = p.choose(4, 5, 30);
        check(k.lookup && k.t == 6, "accepted long matches: the full lookup window is taken");
        check(!p.choose(4, 5, 4).lookup, "buckets are separate: short matches still not taken");
        check(p.choose(4, 20, 30).t <= 6, "never beyond the window cap");
    }
    {
        DraftPolicy p(8);
        for (int i = 0; i < 50; ++i) p.observe(false, 3, 2, 0, cost(3));      // a very good MTP: 3 of 3 tokens
        for (int t = 2; t <= 8; ++t) p.observe(false, t, t - 1, 0, cost(t));
        for (int i = 0; i < 40; ++i) p.observe(true, 4, 2, 8, cost(4));       // lookup at q ~ 0.67
        check(!p.choose(3, 7, 8).lookup, "a mediocre lookup does not replace a strong MTP window");
    }
    {
        DraftPolicy p(6);
        for (int i = 0; i < 50; ++i) p.observe(false, 4, 3, 0, cost(4));      // a near-perfect MTP, only size 4 seen
        const DraftPolicy::Pick k = p.choose(4, 5, 40);
        check(k.lookup && k.t == 6, "an unmeasured size is probed for a confident lookup");
        for (int i = 0; i < 3; ++i) p.observe(true, 6, 5, 40, 3.0 * cost(6));   // it turns out very expensive
        check(!p.choose(4, 5, 40).lookup, "after the probes, the measured cost decides");
    }
    {
        DraftPolicy learned(5);
        simulate(true, 0.f, cost_vram, 5, 4000, &learned);
        const double e1 = std::fabs(learned.accept_rate(0.95f) - true_accept(0.95f)),
                     e2 = std::fabs(learned.accept_rate(0.55f) - true_accept(0.55f));
        std::printf("  c(0.95) %.3f (true %.3f), c(0.55) %.3f (true %.3f)\n", learned.accept_rate(0.95f),
                    true_accept(0.95f), learned.accept_rate(0.55f), true_accept(0.55f));
        // a common bin within 0.05; a rarely reached one within 0.1 (~100 drafts count: a standard error of ~0.05)
        check(e1 < 0.05 && e2 < 0.1, "adaptive: c(p) learns the true acceptance per probability bin");
        for (int max_t : {5, 8}) {
            const double f_cpu = simulate(false, 0.5f, cost_cpu, max_t, 4000),
                         a_cpu = simulate(true, 0.f, cost_cpu, max_t, 4000);
            const double f_vram = simulate(false, 0.5f, cost_vram, max_t, 4000),
                         a_vram = simulate(true, 0.f, cost_vram, max_t, 4000);
            std::printf("  --spec %d: CPU-miss costs %.4f -> %.4f tok/ms (%+.1f%%); VRAM costs %.4f -> %.4f tok/ms "
                        "(%+.1f%%)\n", max_t, f_cpu, a_cpu, 100.0 * (a_cpu / f_cpu - 1.0), f_vram, a_vram,
                        100.0 * (a_vram / f_vram - 1.0));
            check(a_cpu >= 0.99 * f_cpu, "adaptive: no slower than --spec-min-p 0.5 where tokens cost much");
            check(a_vram > 1.02 * f_vram, "adaptive: faster than --spec-min-p 0.5 where tokens are cheap");
        }
    }
    std::printf(g_fail ? "FAIL\n" : "PASS\n");
    return g_fail ? 1 : 0;
}
