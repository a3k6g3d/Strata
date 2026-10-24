import json
import re
import struct
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
SERVER = Path(__file__).resolve().parent / "server.py"

# Piece D of docs/WEB_UI_REDESIGN.md: the page on a phone.  What a test can hold without a phone: the page can be put on a
# home screen (a manifest the server serves, icons of the sizes it names), it runs edge to edge clear of the notch, and the
# behaviours that were mouse-only have a touch path.  The layout itself was checked in the browser at 375 px.


def png_size(p):
    head = p.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{p.name} is not a PNG"
    return struct.unpack(">II", head[16:24])


class HomeScreen(unittest.TestCase):
    def test_the_manifest_names_icons_that_exist_at_their_stated_sizes(self):
        m = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual((m["start_url"], m["display"]), ("/", "standalone"))
        pngs = [i for i in m["icons"] if i["type"] == "image/png"]
        self.assertEqual(sorted(i["sizes"] for i in pngs), ["192x192", "512x512"])   # what Chrome asks for to install
        for i in pngs:
            w, h = (int(x) for x in i["sizes"].split("x"))
            self.assertEqual(png_size(WEB / i["src"].removeprefix("/web/")), (w, h))

    def test_the_page_links_the_manifest_and_an_ios_icon(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="manifest" href="web/manifest.webmanifest">', html)
        icon = re.search(r'<link rel="apple-touch-icon" href="web/([^"]+)">', html)
        self.assertIsNotNone(icon)
        self.assertEqual(png_size(WEB / icon.group(1)), (180, 180))

    def test_the_server_serves_both_file_types(self):
        # the /web/ route refuses an extension it has no type for, so without these the manifest is a 404
        src = SERVER.read_text(encoding="utf-8")
        self.assertIn('".png": "image/png"', src)
        self.assertIn('".webmanifest": "application/manifest+json"', src)


class Phone(unittest.TestCase):
    def test_edge_to_edge_with_the_safe_area_kept_clear(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn("viewport-fit=cover", html)
        css = (WEB / "app.css").read_text(encoding="utf-8")
        for side in ("top", "right", "bottom", "left"):
            self.assertIn(f"env(safe-area-inset-{side}", css)

    def test_select_to_reply_has_a_touch_path(self):
        # a long-press selection sends no mouseup: the selection itself is watched on a touch screen
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.addEventListener("selectionchange"', js)
        self.assertIn('if (lastPointer !== "touch") return;', js)

    def test_the_chat_list_is_never_remembered_open_on_a_narrow_screen(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (!narrow.matches) store.set("sessions_open", on);', js)
        self.assertIn('showSessions(!narrow.matches && store.get("sessions_open", true));', js)


if __name__ == "__main__":
    unittest.main()
