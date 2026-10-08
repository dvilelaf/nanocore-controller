import unittest

from nanocore_controller import __version__


class PackageTests(unittest.TestCase):
    def test_package_exposes_version(self):
        self.assertEqual(__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()


def test_importing_the_library_prints_nothing_without_a_handler(capfd):
    import logging

    import nanocore_controller  # noqa: F401

    logging.getLogger("nanocore.ble").warning("a library warning")
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger("nanocore").handlers)
