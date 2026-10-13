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
        # the panel's own rule (allow_loopback off): the shared web rule, nothing local
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://192.168.1.1/", "http://10.0.0.5/", "file:///c:/windows/win.ini",
                    "javascript:alert(1)", "https://user:pw@example.com/", "http://[::1]/", ""):
            with self.assertRaises(panel.W.WebError, msg=url):
                panel.check(url)

    def test_the_http_ports_only(self):
        with self.assertRaises(panel.W.WebError):
            panel.check("http://example.com:22/")


class ThisPc(unittest.TestCase):
    """With allow_loopback the panel may show a page of this PC - a development server - and nothing else local.
    (No connection is made here: validate only reads the address and resolves a literal, so these are deterministic.)"""

    def test_this_pc_is_let_through(self):
        for url in ("http://127.0.0.1:8443/", "http://127.0.0.1:8080/x", "http://[::1]:8080/", "http://127.0.0.1:3000/"):
            p, port, ip = panel.validate(url, allow_loopback=True)
            self.assertTrue(panel.is_loopback(ip), url)
            self.assertEqual(port, int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[-1]), url)

    def test_any_port_of_this_pc(self):
        # a development server sits on whatever port it likes; the panel may show it on any of them
        for port in (3000, 5173, 8081, 9999):
            p, got, ip = panel.validate(f"http://127.0.0.1:{port}/", allow_loopback=True)
            self.assertEqual(got, port, port)

    def test_the_port_rule_still_applies_to_the_internet(self):
        # the exception is for this PC, not a general loosening of the web rule.  A literal address: with allow_loopback the
        # address is resolved before the port is judged, so a name here would make the test depend on DNS.
        for url in ("http://93.184.216.34:9999/", "http://93.184.216.34:22/"):
            with self.assertRaises(panel.W.WebError) as e:
                panel.validate(url, allow_loopback=True)
            self.assertIn("is not allowed", str(e.exception), url)

    def test_a_public_address_on_a_usual_port_is_still_shown(self):
        # nothing of the old behaviour lost for a page of the internet: literal address, so no DNS either
        p, port, ip = panel.validate("http://93.184.216.34:8080/", allow_loopback=True)
        self.assertEqual(port, 8080)
        self.assertTrue(panel._address(ip).is_global)

    def test_another_machine_of_the_local_network_is_still_refused(self):
        for url in ("http://192.168.1.1/", "http://10.0.0.5:8080/", "http://169.254.1.1/"):
            with self.assertRaises(panel.W.WebError) as e:
                panel.validate(url, allow_loopback=True)
            self.assertIn("not this PC", str(e.exception), url)

    def test_the_loopback_addresses(self):
        for ip in ("127.0.0.1", "127.5.6.7", "::1", "::ffff:127.0.0.1", "::1%eth0"):
            self.assertTrue(panel.is_loopback(ip), ip)
        for ip in ("10.0.0.2", "192.168.0.1", "0.0.0.0", "8.8.8.8"):
            self.assertFalse(panel.is_loopback(ip), ip)

    def test_check_refuses_another_machine_before_reaching_it(self):
        # refused by the rule, not by a failed connection: this would pass for the wrong reason if the order changed
        with self.assertRaises(panel.W.WebError) as e:
            panel.check("http://192.168.1.1/", allow_loopback=True)
        self.assertIn("not this PC", str(e.exception))


if __name__ == "__main__":
    unittest.main()
