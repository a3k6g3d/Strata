"""tools/mtp_from_gguf.py - the MTP draft block from a llama.cpp GGUF that carries it (`blk.N.nextn.*`), in the form
tools/mtp_fetch.py writes, so tools/mtp_pack.py and tools/mtp_rt.py build Strata's draft layer from it unchanged:

    python tools/mtp_from_gguf.py --gguf Qwen3.8-Flash-Next-...-MTP-draft.gguf --out mtp-src
    python tools/mtp_pack.py --src mtp-src --experts q2_0 --out mtp-q2_0.gguf
    python tools/mtp_rt.py --gguf mtp-q2_0.gguf --out rt          (then copy data/draft_vocab.bin into rt/)

For finetunes and abliterations whose MTP head ships only as a GGUF (llama.cpp's qwen4exp converter exports it with
`supports_mtp_export`), so the base checkpoint's head (tools/mtp_fetch.py) would not match the model.

llama.cpp's converter changed the checkpoint's tensors in three ways, undone here (conversion/qwen4exp.py, qwen.py):
  - `eh_proj` = [fc_embedding | fc_hidden] along the input axis: split back;
  - the indexer's `index_qk_proj` was split into q (indexer heads x head dim) and k rows: joined back;
  - every `...norm.weight` had +1 added (zero-centred Gemma gammas): 1 is subtracted again (mtp_rt.py adds it).
The routed experts' gate and up were split from the fused `gate_up_proj` (gate rows first): joined back. Quantized
tensors are dequantized with gguf-py and stored as BF16 (round to nearest even); a Q8_0 source is therefore not the
checkpoint's exact bytes, only within Q8_0's rounding of them. Nothing here runs a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_gguf_py  # noqa: E402
add_gguf_py()
import gguf  # noqa: E402
from gguf.quants import dequantize  # noqa: E402

# checkpoint name -> (GGUF suffix after "blk.N.", how it was changed)
SIMPLE = {
    "mtp.pre_fc_norm_embedding.weight": ("nextn.enorm.weight", "norm"),
    "mtp.pre_fc_norm_hidden.weight": ("nextn.hnorm.weight", "norm"),
    "mtp.hyper_connection_mixer.hc_norm.weight": ("nextn.hc_head_norm.weight", "norm"),
    "mtp.hyper_connection_mixer.input_mix_weight_down.weight": ("nextn.hc_head_down.weight", None),
    "mtp.hyper_connection_mixer.input_mix_weight_up.weight": ("nextn.hc_head_up.weight", None),
    "mtp.layers.0.attn_hyper_connection.hc_norm.weight": ("hc_attn_norm.weight", "norm"),
    "mtp.layers.0.attn_hyper_connection.input_mix_weight_down.weight": ("hc_attn_down.weight", None),
    "mtp.layers.0.attn_hyper_connection.input_mix_weight_up.weight": ("hc_attn_up.weight", None),
    "mtp.layers.0.attn_hyper_connection.block_inject_weight.weight": ("hc_attn_inject.weight", None),
    "mtp.layers.0.mlp_hyper_connection.hc_norm.weight": ("hc_ffn_norm.weight", "norm"),
    "mtp.layers.0.mlp_hyper_connection.input_mix_weight_down.weight": ("hc_ffn_down.weight", None),
    "mtp.layers.0.mlp_hyper_connection.input_mix_weight_up.weight": ("hc_ffn_up.weight", None),
    "mtp.layers.0.mlp_hyper_connection.block_inject_weight.weight": ("hc_ffn_inject.weight", None),
    "mtp.layers.0.mlp.experts.down_proj": ("ffn_down_exps.weight", None),
    "mtp.layers.0.mlp.gate.weight": ("ffn_gate_inp.weight", None),
    "mtp.layers.0.mlp.shared_expert.gate_proj.weight": ("ffn_gate_shexp.weight", None),
    "mtp.layers.0.mlp.shared_expert.up_proj.weight": ("ffn_up_shexp.weight", None),
    "mtp.layers.0.mlp.shared_expert.down_proj.weight": ("ffn_down_shexp.weight", None),
    "mtp.layers.0.mlp.shared_expert_gate.weight": ("ffn_gate_inp_shexp.weight", None),
    "mtp.layers.0.self_attn.q_proj.weight": ("attn_q.weight", None),
    "mtp.layers.0.self_attn.k_proj.weight": ("attn_k.weight", None),
    "mtp.layers.0.self_attn.v_proj.weight": ("attn_v.weight", None),
    "mtp.layers.0.self_attn.o_proj.weight": ("attn_output.weight", None),
    "mtp.layers.0.self_attn.q_norm.weight": ("attn_q_norm.weight", "norm"),
    "mtp.layers.0.self_attn.k_norm.weight": ("attn_k_norm.weight", "norm"),
    "mtp.layers.0.self_attn.indexer.q_layernorm.weight": ("indexer.q_norm.weight", "norm"),
    "mtp.layers.0.self_attn.indexer.k_layernorm.weight": ("indexer.k_norm.weight", "norm"),
}
JOINED = {   # checkpoint name -> (GGUF suffixes joined in order, axis)
    "mtp.layers.0.mlp.experts.gate_up_proj": (("ffn_gate_exps.weight", "ffn_up_exps.weight"), 1),
    "mtp.layers.0.self_attn.indexer.index_qk_proj.weight": (("indexer.q_proj.weight", "indexer.k_proj.weight"), 0),
}
EH_PROJ = "nextn.eh_proj.weight"   # -> mtp.fc_embedding.weight (input columns [0, H)), mtp.fc_hidden.weight ([H, 2H))


def to_bf16(x: np.ndarray) -> np.ndarray:
    """float32 -> BF16 bits, round to nearest even (NaN kept quiet)."""
    x = np.ascontiguousarray(x, dtype=np.float32)
    u = x.view(np.uint32)
    r = ((u >> 16) & 1) + 0x7FFF
    out = ((u + r) >> 16).astype(np.uint16)
    nan = np.isnan(x)
    if nan.any():
        out[nan] = 0x7FC0
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gguf", required=True, help="a llama.cpp GGUF with the blk.N.nextn.* MTP block")
    ap.add_argument("--out", required=True, help="directory for the BF16 tensors and mtp-manifest.json")
    a = ap.parse_args()
    r = gguf.GGUFReader(a.gguf)
    tens = {t.name: t for t in r.tensors}
    blocks = sorted({int(n.split(".")[1]) for n in tens if n.startswith("blk.") and ".nextn." in n})
    if len(blocks) != 1:
        print(f"{a.gguf}: expected one block with nextn.* tensors, found {blocks or 'none'}", file=sys.stderr)
        return 2
    pre = f"blk.{blocks[0]}."

    def load(suffix: str) -> np.ndarray:
        t = tens.get(pre + suffix)
        if t is None:
            raise KeyError(f"{a.gguf}: no tensor {pre + suffix}")
        return np.asarray(dequantize(t.data, t.tensor_type), dtype=np.float32)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []

    def put(name: str, x: np.ndarray, how: str):
        bf = to_bf16(x)
        path = out / (name + ".bf16")
        path.write_bytes(bf.tobytes())
        manifest.append(dict(name=name, dtype="BF16", shape=list(x.shape), file=path.name,
                             sha256=hashlib.sha256(bf.tobytes()).hexdigest(), source=how))
        print(f"{name:66s} {'x'.join(map(str, x.shape)):>16s}  {how}", flush=True)

    for name, (suffix, kind) in SIMPLE.items():
        x = load(suffix)
        if kind == "norm":
            # every one was renamed to a "...norm.weight" (pre_fc_norm_* to enorm / hnorm) before qwen.py's +1 rule
            x = x - 1.0
        put(name, x, pre + suffix + (" - 1" if kind == "norm" else ""))
    for name, (parts, axis) in JOINED.items():
        put(name, np.concatenate([load(p) for p in parts], axis=axis), " + ".join(pre + p for p in parts))
    eh = load(EH_PROJ)
    h = eh.shape[1] // 2
    if eh.shape[1] != 2 * h or eh.shape[0] != h:
        print(f"{pre + EH_PROJ}: shape {eh.shape}, expected (H, 2H)", file=sys.stderr)
        return 2
    put("mtp.fc_embedding.weight", eh[:, :h], pre + EH_PROJ + f"[:, :{h}]")
    put("mtp.fc_hidden.weight", eh[:, h:], pre + EH_PROJ + f"[:, {h}:]")
    (out / "mtp-manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"{len(manifest)} tensors -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
