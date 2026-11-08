import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.harness import ROOT  # noqa: F401

from opencloddie.config import (
    build_default_config,
    init_config,
    load_config,
    write_config,
)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencloddie-config-")
        self.path = os.path.join(self.tmp, "config.json")
        self._old_state = os.environ.get("OPENCLODDIE_STATE_DIR")
        os.environ["OPENCLODDIE_STATE_DIR"] = os.path.join(self.tmp, "state")

    def tearDown(self):
        if self._old_state is None:
            os.environ.pop("OPENCLODDIE_STATE_DIR", None)
        else:
            os.environ["OPENCLODDIE_STATE_DIR"] = self._old_state

    def test_build_defaults(self):
        cfg = build_default_config()
        self.assertTrue(cfg["secrets"]["master"])
        self.assertEqual(cfg["server"]["web_port"], 8787)
        self.assertEqual(cfg["server"]["api_port"], 8788)
        self.assertEqual(cfg["server"]["behind_proxy"], False)
        self.assertEqual(cfg["web"]["dev_user"], "")
        self.assertGreaterEqual(len(cfg["gateway"]["models"]), 3)

    def test_init_and_load(self):
        init_config(self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        cfg = load_config(self.path)
        self.assertEqual(cfg["_config_path"], self.path)
        self.assertTrue(Path(cfg["_state_dir"]).is_dir())
        self.assertTrue(Path(cfg["database"]["path"]).parent.is_dir())
        self.assertTrue(cfg["secrets"]["master"])

    def test_init_refuses_overwrite_unless_forced(self):
        init_config(self.path)
        with self.assertRaises(FileExistsError):
            init_config(self.path)
        init_config(self.path, force=True)

    def test_missing_master_is_rejected(self):
        cfg = build_default_config()
        cfg["secrets"]["master"] = ""
        write_config(self.path, cfg)
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_load_missing(self):
        with self.assertRaises(FileNotFoundError):
            load_config(os.path.join(self.tmp, "nope.json"))

    def test_docker_defaults(self):
        cfg = build_default_config(dev=False)
        self.assertEqual(cfg["web"]["dev_user"], "")


if __name__ == "__main__":
    unittest.main()
