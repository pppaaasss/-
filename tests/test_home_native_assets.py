import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import home_native_assets as assets


class NativeInboxEnvironmentTests(unittest.TestCase):
    def check_count(self, entries, expected):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = directory / 'environment'
            release = {'tag_name': assets.TAG, 'draft': True, 'id': 123}

            def download(command, **kwargs):
                kwargs['stdout'].write(b'sample')

            with patch.dict(os.environ, GITHUB_ENV=str(environment)), \
                    patch.object(assets, 'api', side_effect=[[release], entries]), \
                    patch.object(assets.subprocess, 'run', side_effect=download) as run:
                assets.prepare(directory)
            values = dict(line.split('=', 1) for line in environment.read_text().splitlines())
            self.assertEqual(values['IPTV_NATIVE_ASSET_COUNT'], str(expected))
            self.assertEqual(values['IPTV_NATIVE_RELEASE_ID'], '123')
            self.assertEqual(values['IPTV_NATIVE_INBOX'], str(directory))
            self.assertEqual(len(json.loads((directory / 'asset-index.json').read_text())), expected)
            self.assertEqual(run.call_count, expected)

    def test_empty_inbox_does_not_require_decoder(self):
        self.check_count([], 0)

    def test_unrelated_assets_do_not_require_decoder(self):
        self.check_count([{'name': 'README.txt', 'size': 6, 'id': 1}], 0)

    def test_sample_inbox_requires_decoder_before_ingestion(self):
        self.check_count([{'name': 'a' * 64 + '-' + 'b' * 64 + '.native',
                           'size': 6, 'id': 1}], 1)


if __name__ == '__main__':
    unittest.main()
