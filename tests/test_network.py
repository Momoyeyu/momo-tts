from unittest import TestCase
from unittest.mock import patch

from momo_tts.network import console_address


class ConsoleNetworkTest(TestCase):
    def test_ssh_interface_comes_before_default_route(self):
        self.assertEqual(console_address("auto", "10.1.2.3 49152 10.32.4.5 22"), "10.32.4.5")

    def test_explicit_interface_and_local_mode(self):
        self.assertEqual(console_address("192.168.4.2"), "192.168.4.2")
        self.assertEqual(console_address("loopback"), "")
        for address in ("0.0.0.0", "8.8.8.8", "localhost", "100.64.1.1"):
            with self.assertRaises(ValueError):
                console_address(address)

    @patch("momo_tts.network.socket.socket")
    def test_auto_does_not_choose_a_public_default_interface(self, socket):
        probe = socket.return_value.__enter__.return_value
        for address, expected in (("172.18.0.12", "172.18.0.12"), ("8.8.8.8", "")):
            probe.getsockname.return_value = (address, 24000)
            self.assertEqual(console_address("auto"), expected)
