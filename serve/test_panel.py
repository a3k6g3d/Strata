import unittest

from serve import panel


class H(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class FrameRules(unittest.TestCase):
    def test_x_frame_options(self):
        self.assertTrue(panel.frameable_from_headers(H())[0])
        for v in ("DENY", "SAMEORIGIN", "deny", "ALLOW-FROM https://x.test"):
            ok, why = panel.frameable_from_headers(H({"X-Frame-Options": v}))
            self.assertFalse(ok, v)
            self.assertIn("X-Frame-Options", why)

    def test_frame_ancestors(self):
        parent = "http://127.0.0.1:8080"
        for csp, expect in (("default-src 'self'", True), ("frame-ancestors 'none'", False), ("frame-ancestors 'self'", False),
                            ("script-src a; frame-ancestors *", True), ("frame-ancestors https:", True),
                            ("frame-ancestors https://other.test", False), (f"frame-ancestors {parent}", True)):
            self.assertEqual(panel.frameable_from_headers(H({"Content-Security-Policy": csp}), parent)[0], expect, csp)


class Refusals(unittest.TestCase):
    def test_local_and_odd_addresses_are_refused(self):
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://192.168.1.1/", "http://10.0.0.5/", "file:///c:/windows/win.ini",
                    "javascript:alert(1)", "https://user:pw@example.com/", "http://[::1]/", ""):
            with self.assertRaises(panel.W.WebError, msg=url):
                panel.check(url)

    def test_the_http_ports_only(self):
        with self.assertRaises(panel.W.WebError):
            panel.check("http://example.com:22/")


if __name__ == "__main__":
    unittest.main()
