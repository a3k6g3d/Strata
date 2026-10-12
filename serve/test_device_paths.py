"""A raw device name in a command (\\\\.\\PhysicalDrive4, smartctl's argument) is not a file: the no-read scan lets it pass, and auto mode
makes a command that reads a raw disk ask, because a raw read skips the file rules."""
import os
import unittest

from serve import auto_mode, mcp

DEV = "\\\\.\\PhysicalDrive4"


class DevicePaths(unittest.TestCase):
    def blocks(self):
        return mcp.BlockList([os.path.join(os.environ.get("TEMP", "C:\\Temp"), "strata_secret")])

    def test_a_device_name_does_not_trip_the_no_read_scan(self):
        bl = self.blocks()
        self.assertFalse(bl.blocks_text('smartctl.exe -a -d sat "' + DEV + '" 2>&1'))
        self.assertFalse(bl.blocks_text("Get-Disk | Format-List; smartctl -a '" + DEV + "'"))

    def test_a_real_blocked_path_still_does(self):
        bl = self.blocks()
        secret = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "strata_secret", "k.txt")
        self.assertTrue(bl.blocks_text('type "' + secret + '"'))
        self.assertTrue(bl.blocks_text('type "\\\\?\\' + secret + '"'))      # the \\?\ form of a drive path is still resolved

    def test_a_raw_disk_read_asks_in_auto_mode(self):
        why = auto_mode.assess_exec({"command": 'smartctl.exe -a "' + DEV + '"'})
        self.assertIn("raw disk", why or "")
        self.assertIsNone(auto_mode.assess_exec({"command": "Get-Disk | Format-List"}))


if __name__ == "__main__":
    unittest.main()
