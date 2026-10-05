// include/strata/spec/draft_policy.hpp - per verify round: the MTP's window, or a lookup (suffix) window?
//
// The suffix drafter (prompt lookup) proposes the tokens that followed an earlier repeat of the context. Taken
// whenever it proposes more than the MTP, it lost 2-8% on ordinary text: a long lookup window costs much more to
// verify than the MTP's usual 3-4 tokens, and it is only worth that when enough of it is accepted. llama.cpp's
// lookup decoding answers the same problem with confidence thresholds on its n-gram statistics; here the policy
// learns both sides online and compares expected committed tokens per millisecond:
//
//   MTP     E = the mean tokens a window of that size has committed (EMA), at the measured cost of that size
//   lookup  E(k) = 1 + q + q^2 + ... + q^k for k <= the proposal, q = the acceptance rate of lookup drafts whose
//           match was about as long (4 buckets of match length, decayed counts), at the measured cost of k + 1
//
// and takes the lookup window only when its best E/cost beats the MTP's by `margin`. Costs are the measured round
// times per window size (EMA; sizes not seen yet are scaled from seen ones by a prior shape), so the policy adapts
// to the machine and the context length. It only chooses which drafts to verify: the output is unchanged.
//
// The MTP's own window (`--spec-adaptive`, choose_mtp): instead of every draft whose probability is at least
// `--spec-min-p`, the length with the most expected committed tokens per millisecond:
//
//   E(T) = 1 + c(p1) + c(p1) c(p2) + ... + c(p1)...c(p_{T-1})     c(p) = how often a draft of probability p was
//                                                                   accepted when reached (10 bins, decayed counts)
//
// at the measured round cost of T (as above). On a PC where a token more costs little (every expert in VRAM) it
// verifies deeper; where it costs much (missed experts on the CPU) it stops earlier than a fixed threshold would.
#pragma once

#include <array>

namespace strata::spec {

class DraftPolicy {
public:
    static constexpr int kMaxT = 8;
    static constexpr int kBuckets = 4;

    explicit DraftPolicy(int max_t, double margin = 0.03);

    struct Pick {
        bool lookup = false;
        int t = 1;                      // window size (1 + drafts)
    };
    /// `t_mtp`: the MTP's window; `lookup_k`: the lookup proposal's length (0 = none); `match`: its match length.
    Pick choose(int t_mtp, int lookup_k, int match) const;
    /// After the round: the window it used, the drafts accepted, and the round's time (verify + commit + draft).
    void observe(bool lookup, int t, int accepted, int match, double round_ms);

    double lookup_rate(int match) const;   // current q for a match length
    double cost_ms(int t) const;           // measured or scaled round time of a window of t tokens

    /// --spec-adaptive: the MTP window (1 + drafts used) for the chain's draft probabilities `probs[0..n_drafts)`,
    /// at most `max_t`. A size whose cost was not measured yet is tried a few times when its guessed rate is close
    /// to the best, so the cost table fills where it matters.
    int choose_mtp(const float* probs, int n_drafts, int max_t) const;
    /// After an MTP window of `t` (its drafts' probabilities `probs[0..t-1)`) that accepted `accepted` drafts.
    void observe_drafts(const float* probs, int t, int accepted);
    double accept_rate(float p) const;     // c(p): the acceptance of a reached draft of probability p
    static constexpr int kPBins = 10;
    /// c(p) per bin, for MtpDrafter::draft's `accept` (the chain stops where no window would use its next draft).
    void accept_table(float out[kPBins]) const;
    /// The draft chain in adaptive mode goes on while the last draft is at least kChainFloor likely and the chance
    /// that every draft so far is accepted is at least kChainReach (the simulated best of floors 0.2-0.4 x reaches
    /// 0-0.5 over two cost curves, draft_policy_test).
    static constexpr float kChainFloor = 0.3f;
    static constexpr float kChainReach = 0.35f;

private:
    static int bucket(int match);
    static int pbin(float p);
    double mtp_tokens(int t) const;
    double expected_tokens(const float* probs, int t) const;

    int max_t_;
    double margin_;
    std::array<double, kMaxT + 1> cost_{}, cost_n_{};      // round ms by window size
    std::array<double, kMaxT + 1> mtp_tok_{}, mtp_n_{};    // tokens committed by MTP windows of that size
    std::array<double, kBuckets> ok_{}, bad_{};            // lookup drafts accepted / windows cut short, decayed
    std::array<double, kPBins> p_ok_{}, p_bad_{};          // MTP drafts reached by probability bin: accepted / not
};

}  // namespace strata::spec
