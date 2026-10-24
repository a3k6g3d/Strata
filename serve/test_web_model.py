import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
APP = WEB / "app.js"

# Piece B of docs/WEB_UI_REDESIGN.md: the model card (name without its quantization, the quantization as a label) and the
# composer's Think chip, which is the same setting as Thinking under Sampling.  modelQuant is run in node.
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const i = src.indexOf('function modelQuant(');
let d = 0, end = -1;
for (let k = src.indexOf('{', i); k < src.length; k++) {
  if (src[k] === '{') d++;
  else if (src[k] === '}' && --d === 0) { end = k + 1; break; }
}
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src.slice(i, end), ctx);
console.log(JSON.stringify(JSON.parse(process.argv[2]).map((n) => {
  const q = vm.runInContext('modelQuant(' + JSON.stringify(n) + ')', ctx);
  return [q.label, q.at > 0 ? n.slice(0, q.at) : n];
})));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ModelCard(unittest.TestCase):
    def quant(self, *names):
        r = subprocess.run(["node", "-e", HARNESS, str(APP), json.dumps(names)], capture_output=True, text=True,
                           encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [tuple(x) for x in json.loads(r.stdout)]

    def test_the_live_model_name(self):
        self.assertEqual(self.quant("qwen3.8-flash-next-swift15-abliterated-iq3s"),
                         [("IQ3_S", "qwen3.8-flash-next-swift15-abliterated")])

    def test_gguf_style_names_and_split_file_suffixes(self):
        self.assertEqual(self.quant("Model-Q4_K_M-00001-of-00002", "x.IQ2_XXS", "a-q8_0"),
                         [("Q4_K_M", "Model"), ("IQ2_XXS", "x"), ("Q8_0", "a")])

    def test_a_name_without_a_quantization_is_left_alone(self):
        # "qwen3" starts with q and a letter, not q and a digit: not a quantization
        self.assertEqual(self.quant("qwen3.8-flash-next", "strata"), [("", "qwen3.8-flash-next"), ("", "strata")])


class ThinkChip(unittest.TestCase):
    def test_the_chip_offers_the_same_levels_as_the_sampling_dialog(self):
        import re
        html = (WEB / "index.html").read_text(encoding="utf-8")
        chip = re.findall(r'<option value="(\w+)"', html[html.index('id="think-select"'):html.index("</select>", html.index('id="think-select"'))])
        seg = re.search(r'<div class="seg" id="s-thinking".*?</div>', html, re.S).group(0)
        self.assertEqual(chip, re.findall(r'data-v="(\w+)"', seg))

    def test_both_write_the_one_setting_the_request_sends(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn('settings = {...settings, thinking: $("think-select").value};', js)
        self.assertIn('$("think-select").value = settings.thinking;', js)
        self.assertIn("reasoning_effort: settings.thinking", js)


if __name__ == "__main__":
    unittest.main()
