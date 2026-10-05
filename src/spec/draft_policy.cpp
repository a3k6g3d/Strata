// src/spec/draft_policy.cpp - see include/strata/spec/draft_policy.hpp.
#include "strata/spec/draft_policy.hpp"

#include <algorithm>

namespace strata::spec {
namespace {

// The shape of a round's cost by window size, relative to one token, used only for sizes not measured yet (the
// measured round times replace it). Between the RTX 5070's measured curves: +10 ms per token with every missed expert
// on the CPU (bench/results/2026-09-27-spec/window-cost), flatter with the default CPU/DMA split.
constexpr double kShape[DraftPolicy::kMaxT + 1] = {0.0, 1.0, 1.35, 1.7, 2.05, 2.45, 2.85, 3.25, 3.6};
constexpr double kCostAlpha = 0.1;    // EMA weight of a new round time
constexpr double kTokAlpha = 0.05;    // EMA weight of a new MTP window outcome
constexpr double kDecay = 0.97;       // lookup counts: older windows fade
// Before a bucket has data: the longer the match, the likelier its continuation (llama.cpp's lookup decoding gates
// on the same thing); worth 4 observations, so a few real windows override it
constexpr double kPriorQ[DraftPolicy::kBuckets] = {0.75, 0.88, 0.93, 0.96};
constexpr double kPriorN = 4.0;
constexpr int kProbes = 3;       // a lookup window size is tried this often before its guessed cost can veto it
constexpr double kPriorPN = 3.0;      // c(p) before data: p itself (a calibrated head), worth 3 drafts
constexpr double kPDecay = 0.99;      // per bin and draft: about the last 100 drafts of that probability count
constexpr double kProbeNear = 0.9;    // an unmeasured MTP size within this share of the best rate is probed

}  // namespace

DraftPolicy::DraftPolicy(int max_t, double margin)
    : max_t_(std::clamp(max_t, 1, kMaxT)), margin_(margin) {}

int DraftPolicy::bucket(int match) {
    return match < 6 ? 0 : match < 12 ? 1 : match < 24 ? 2 : 3;
}

double DraftPolicy::lookup_rate(int match) const {
    const int b = bucket(match);
    return (ok_[b] + kPriorN * kPriorQ[b]) / (ok_[b] + bad_[b] + kPriorN);
}

double DraftPolicy::cost_ms(int t) const {
    t = std::clamp(t, 1, kMaxT);
    if (cost_n_[t] > 0) return cost_[t];
    // scale from the measured sizes, weighting each by how often it was seen
    double num = 0.0, den = 0.0;
    for (int u = 1; u <= kMaxT; ++u)
        if (cost_n_[u] > 0) {
            const double w = std::min(cost_n_[u], 20.0);
            num += w * cost_[u] * kShape[t] / kShape[u];
            den += w;
        }
    return den > 0 ? num / den : kShape[t];
}

int DraftPolicy::pbin(float p) {
    return std::clamp((int) (p * kPBins), 0, kPBins - 1);
}

double DraftPolicy::accept_rate(float p) const {
    const int b = pbin(p);
    const double prior = (b + 0.5) / kPBins;
    return (p_ok_[b] + kPriorPN * prior) / (p_ok_[b] + p_bad_[b] + kPriorPN);
}

void DraftPolicy::accept_table(float out[kPBins]) const {
    for (int b = 0; b < kPBins; ++b) out[b] = (float) accept_rate((b + 0.5f) / kPBins);
}

double DraftPolicy::expected_tokens(const float* probs, int t) const {
    double e = 1.0, reach = 1.0;
    for (int i = 0; i < t - 1; ++i) {
        reach *= accept_rate(probs[i]);
        e += reach;
    }
    return e;
}

int DraftPolicy::choose_mtp(const float* probs, int n_drafts, int max_t) const {
    const int hi = std::clamp(std::min(max_t, n_drafts + 1), 1, max_t_);
    int best_t = 1;
    double best = expected_tokens(probs, 1) / cost_ms(1);
    for (int t = 2; t <= hi; ++t) {
        const double r = expected_tokens(probs, t) / cost_ms(t);
        if (r > best) { best = r; best_t = t; }
    }
    // the deepest size not measured enough whose guessed rate is near the best: try it (only speed is at stake)
    for (int t = hi; t > best_t; --t)
        if (cost_n_[t] < kProbes && expected_tokens(probs, t) / cost_ms(t) >= kProbeNear * best) return t;
    return best_t;
}

void DraftPolicy::observe_drafts(const float* probs, int t, int accepted) {
    // a bin fades only as it gets new drafts: a rarely reached probability keeps its (few) observations
    auto add = [&](float p, bool ok) {
        const int b = pbin(p);
        p_ok_[b] = kPDecay * p_ok_[b] + (ok ? 1.0 : 0.0);
        p_bad_[b] = kPDecay * p_bad_[b] + (ok ? 0.0 : 1.0);
    };
    // drafts 1..accepted were accepted; the next one, if the window had it, was reached and rejected
    for (int i = 0; i < std::min(accepted, t - 1); ++i) add(probs[i], true);
    if (accepted < t - 1) add(probs[accepted], false);
}

double DraftPolicy::mtp_tokens(int t) const {
    if (mtp_n_[t] > 0) return mtp_tok_[t];
    return 1.0 + 0.7 * (t - 1);       // before any MTP window of this size: a typical acceptance
}

DraftPolicy::Pick DraftPolicy::choose(int t_mtp, int lookup_k, int match) const {
    Pick p;
    p.t = std::clamp(t_mtp, 1, max_t_);
    if (lookup_k <= 0) return p;
    const double base = mtp_tokens(p.t) / cost_ms(p.t);
    const double q = lookup_rate(match);
    double e = 1.0, qi = 1.0, best = 0.0;
    int best_t = 0;
    for (int k = 1; k <= std::min(lookup_k, max_t_ - 1); ++k) {
        qi *= q;
        e += qi;
        const double r = e / cost_ms(k + 1);
        if (r > best) { best = r; best_t = k + 1; }
    }
    if (best_t > 0 && best > base * (1.0 + margin_)) {
        p.lookup = true;
        p.t = best_t;
        return p;
    }
    // a guessed cost can keep the policy from ever measuring a size: the first few times a confident lookup would
    // need a size not measured yet, it is tried (verification keeps the output; only the one round's speed is at stake)
    const int t_full = std::min(lookup_k, max_t_ - 1) + 1;
    if (t_full > p.t && cost_n_[t_full] < kProbes && q >= 0.85) {
        p.lookup = true;
        p.t = t_full;
    }
    return p;
}

void DraftPolicy::observe(bool lookup, int t, int accepted, int match, double round_ms) {
    t = std::clamp(t, 1, kMaxT);
    if (round_ms > 0) {
        cost_[t] = cost_n_[t] > 0 ? (1.0 - kCostAlpha) * cost_[t] + kCostAlpha * round_ms : round_ms;
        cost_n_[t] += 1.0;
    }
    if (lookup) {
        const int b = bucket(match);
        ok_[b] = kDecay * ok_[b] + accepted;
        bad_[b] = kDecay * bad_[b] + (accepted < t - 1 ? 1.0 : 0.0);
    } else {
        const double got = accepted + 1.0;
        mtp_tok_[t] = mtp_n_[t] > 0 ? (1.0 - kTokAlpha) * mtp_tok_[t] + kTokAlpha * got : got;
        mtp_n_[t] += 1.0;
    }
}

}  // namespace strata::spec
