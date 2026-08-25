import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clean_single_station.py"
SPEC = importlib.util.spec_from_file_location("clean_single_station", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CleanSingleStationTests(unittest.TestCase):
    def test_clean_preserves_the_selected_cctv5_route(self):
        selected = "http://207.56.13.146:81/cdnlive/cctv5.m3u8"
        text = "\n".join(
            (
                "#EXTM3U",
                "# channels=1",
                '#EXTINF:-1 group-title="卫视台",CCTV-5',
                selected,
                "",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.m3u"
            path.write_text(text, encoding="utf-8")
            MODULE.clean(path)
            rendered = path.read_text(encoding="utf-8")
        self.assertIn(selected, rendered)
        self.assertNotIn("219.140.56.34", rendered)


if __name__ == "__main__":
    unittest.main()
