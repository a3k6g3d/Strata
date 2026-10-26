"""tools/ui/sprite_usage_check.py - which sprite symbols nothing asks for any more.

    python tools/ui/sprite_usage_check.py        # tiers every symbol by the kind of evidence found

Not a name grep. A symbol is used in one of three ways and each has to be looked for separately:

  static   `<use href="web/sprite.svg#i-foo">` in html/css/js          - certain
  call     icon("foo", ...) in our own web js, which emits `#i-` + name - certain
  dynamic  built at runtime, e.g. the theme toggle's `#i-${t == "dark" ? "sun" : "moon"}` - counted as used

Only serve/web and docs/media are searched: the vendored upstream app under build-cpu-release/_deps
has its own icons and is not this sprite's consumer. And a bare word is not evidence - "download" is an
HTML attribute, "clock" is a CSS class - so the bare form is accepted only in an icon(...) call.

An unused symbol is a report, not a failure: the sprite is hand-tended and a symbol can be waiting for
the screen that will use it. So this prints, it does not fail - no exit code, no CI hook.

Do NOT trim the <metadata> block to save bytes - it is a C2PA provenance manifest for the icon set
(~27% of the file, and the only part a browser fetch would happily drop). It is attribution. Keep it.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPRITE = ROOT / "serve" / "web" / "sprite.svg"
SEARCH = [ROOT / "serve" / "web", ROOT / "docs" / "media"]
EXTS = {".html", ".js", ".css", ".py", ".md", ".json", ".svg"}

sprite = SPRITE.read_text(encoding="utf-8")
ids = re.findall(r'<symbol id="([^"]+)"', sprite)

files = [p for d in SEARCH for p in d.rglob("*") if p.is_file() and p.suffix.lower() in EXTS and p != SPRITE]
texts = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8", errors="ignore") for p in files}
where = sorted(texts)

# what the sprite's own generators and the dynamic builders reference, kept separate from the certain tiers
dynamic = re.compile(r"#i-\$\{")
tiers = {"static": [], "call": [], "prop": [], "dynamic": [], "unused": []}
for i in ids:
    bare = i[2:] if i.startswith("i-") else i
    q = re.escape(bare)
    qa = re.escape(i)          # the sprite's own generators pass the full id, the app passes the bare name
    hit_static = [f for f in where if "#" + i in texts[f]]
    hit_call = [f for f in where if re.search(r'\bicon\(\s*["\'`](?:' + q + '|' + qa + ')["\'`]', texts[f])]
    hit_prop = [f for f in where if re.search(r'\bicon[a-z_]*:\s*["\'`](?:' + q + '|' + qa + ')["\'`]', texts[f])]
    # the name has to sit *next to* the builder, not merely in the same file: app.js is full of
    # quoted "error"/"warn" state strings that are toast levels, not icon names
    hit_dyn = [f for f in where if re.search(r'["\'`](?:' + q + '|' + qa + ')["\'`].{0,40}#i-\$\{', texts[f])
               or re.search(r'#i-\$\{.{0,60}["\'`][\'"]?(?:' + q + '|' + qa + ')', texts[f])]
    for tier, hits in (("static", hit_static), ("call", hit_call), ("prop", hit_prop), ("dynamic", hit_dyn)):
        if hits:
            tiers[tier].append((i, hits))
            break
    else:
        tiers["unused"].append((i, []))

for name in ("static", "call", "prop", "dynamic", "unused"):
    print(f"{name:8s} {len(tiers[name]):2d}")
print()
for name in ("dynamic", "unused"):
    for i, where in tiers[name]:
        print(f"  {name:8s} {i:14s} {', '.join(sorted(set(where))[:3]) or '-'}")
sys.exit(0)
