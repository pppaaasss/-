from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from scripts import viewer_locked_channels as module


class ViewerLockedChannelsTests(unittest.TestCase):
    def config(self) -> dict:
        return {
            "channels": [
                {
                    "name": "龙华电影",
                    "group": "台湾",
                    "logo": "https://example.test/longhua.png",
                    "preferred_url": "https://example.test/longhua.m3u8",
                    "playlists": ["tv.m3u", "tv-all.m3u", "tv-easy.m3u"],
                }
            ]
        }

    def test_restores_missing_channel_without_deleting_anything(self) -> None:
        playlist = "\n".join((
            "#EXTM3U",
            '#EXTINF:-1 group-title="台湾",台视',
            "https://example.test/ttv.m3u8",
            '#EXTINF:-1 group-title="香港",翡翠台',
            "https://example.test/jade.m3u8",
            "",
        ))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            added = module.ensure_path(path, self.config())
            rendered = path.read_text(encoding="utf-8")

        self.assertEqual(added, ["龙华电影"])
        self.assertIn("台视", rendered)
        self.assertIn("翡翠台", rendered)
        self.assertIn("龙华电影", rendered)
        self.assertIn("https://example.test/longhua.m3u8", rendered)

    def test_existing_failover_route_is_preserved_and_not_duplicated(self) -> None:
        alternate = "https://example.test/failover.m3u8"
        playlist = "\n".join((
            "#EXTM3U",
            '#EXTINF:-1 group-title="台湾",龙华电影',
            alternate,
            "",
        ))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tv.m3u"
            path.write_text(playlist, encoding="utf-8")
            added = module.ensure_path(path, self.config())
            rendered = path.read_text(encoding="utf-8")

        self.assertEqual(added, [])
        self.assertEqual(rendered.count("龙华电影"), 1)
        self.assertIn(alternate, rendered)
        self.assertNotIn("https://example.test/longhua.m3u8", rendered)

    def test_repository_config_excludes_core_playlist(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        data = json.loads(
            (root / "config/viewer-locked-channels.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(data["channels"]), 7)
        for row in data["channels"]:
            self.assertNotIn("tv-core.m3u", row["playlists"])
            self.assertEqual(
                row["playlists"],
                ["tv-easy.m3u", "tv.m3u", "tv-all.m3u"],
            )

    def test_locked_channels_are_present_once_in_each_formal_target(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        data = json.loads(
            (root / "config/viewer-locked-channels.json").read_text(encoding="utf-8")
        )
        for row in data["channels"]:
            for playlist in row["playlists"]:
                text = (root / playlist).read_text(encoding="utf-8")
                visible = [
                    line.rsplit(",", 1)[-1].strip()
                    for line in text.splitlines()
                    if line.startswith("#EXTINF") and "," in line
                ]
                self.assertEqual(
                    visible.count(row["name"]),
                    1,
                    f'{playlist} must contain one {row["name"]}',
                )


if __name__ == "__main__":
    unittest.main()
