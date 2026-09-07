import tempfile
from pathlib import Path
import unittest

from router.ac86u.rules_check import inspect, providers, scan


class RulesCheckTests(unittest.TestCase):
    def test_provider_section_excludes_proxy_paths_and_later_rules(self):
        blocks, unknown = providers('''proxy-providers:
  subscription:
    path: secret.yaml
rule-providers:
  first:
    behavior: classical
    path: './rules/a.yaml' # local file
    url: https://private.invalid/token
    header:
      Authorization:
        - secret
  second:
    behavior: ipcidr
    path: rules/b.yaml
rules:
  - MATCH,DIRECT
''')
        self.assertEqual(0, unknown)
        self.assertEqual(2, len(blocks))
        self.assertEqual('./rules/a.yaml', blocks[0]['path'])
        self.assertNotIn('url', blocks[0])

    def test_nested_source_conditions_count_without_values(self):
        types, source, unknown, rows = scan('''payload:
  - DOMAIN-SUFFIX,example.test
  - 'AND,((SRC-IP-CIDR,192.168.50.167/32),(DST-PORT,443))'
  - PROCESS-NAME,private-app
  - RULE-SET,nested-private-provider
''', 'classical', 'yaml')
        self.assertEqual(4, rows)
        self.assertEqual({'SRC-IP-CIDR': 1, 'PROCESS-NAME': 1}, source)
        self.assertEqual(1, unknown)
        self.assertNotIn('private', str(types) + str(source))

    def test_unsupported_yaml_and_binary_do_not_appear_complete(self):
        self.assertGreater(providers('rule-providers: {x: {path: a}}')[1], 0)
        self.assertGreater(providers('rule-providers:\n  x:\n    <<: *shared\n')[1], 0)
        for text, fmt in [('payload: [DOMAIN,example.test]', 'yaml'), ('\0abc', 'yaml'), ('binary', 'mrs')]:
            self.assertGreater(scan(text, 'classical', fmt)[2], 0)

    def test_relative_absolute_missing_and_plain_text_files(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / 'rules.yaml').write_text('payload:\n  - DOMAIN,example.test\n')
            (base / 'cidrs.txt').write_text('192.0.2.0/24\n')
            config = base / 'config.yaml'
            config.write_text('rule-providers:\n  a:\n    path: rules.yaml\n    behavior: classical\n'
                              '  b:\n    path: ' + str(base / 'cidrs.txt') + '\n    behavior: ipcidr\n    format: text\n'
                              '  c:\n    path: absent.yaml\n    behavior: classical\n')
            result = inspect(config, base)
            self.assertEqual(3, result['providers'])
            self.assertEqual(2, result['read_files'])
            self.assertEqual(1, result['missing_or_unsupported'])
            self.assertEqual(2, result['rule_rows'])
            self.assertEqual({}, result['source_sensitive'])


if __name__ == '__main__':
    unittest.main()
