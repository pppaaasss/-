#!/opt/bin/python3
"""Read-only screening of local rule-provider files; never certifies a route.

Supports ordinary block YAML provider declarations and yaml/text payloads.
Unsupported structures, missing files and nested rule references stay visible.
Does not import YAML dependencies, contact an API, or print rule values/secrets.
"""
import collections
import json
from pathlib import Path
import re


def scalar(value):
    value = value.strip()
    if value.startswith('"'):
        result, end = json.JSONDecoder().raw_decode(value)
        if value[end:].strip() and not value[end:].lstrip().startswith('#'):
            raise ValueError('scalar suffix')
        return result
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ValueError('quoted scalar')
        return match[1].replace("''", "'")
    value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
    if not value or value[0] in '&*!{|[>':
        raise ValueError('unsupported scalar')
    return value


def providers(text):
    blocks, current, active, indent, unknown = [], None, False, None, 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if re.fullmatch(r'rule-providers:\s*(?:#.*)?', line):
            active = True
            continue
        if not active:
            continue
        if not line[0].isspace():
            break
        depth = len(line) - len(line.lstrip(' '))
        if indent is None:
            indent = depth
        if depth == indent:
            current = {}
            blocks.append(current)
            if not re.fullmatch(r'\s+.+:\s*(?:#.*)?', line):
                unknown += 1
        elif current is not None:
            match = re.match(r'\s+(behavior|path|format|type):\s*(.*)$', line)
            if match:
                key, raw = match.groups()
                try:
                    if key in current:
                        raise ValueError('duplicate field')
                    current[key] = scalar(raw)
                except (ValueError, TypeError):
                    unknown += 1
            if line.lstrip().startswith('<<:'):
                unknown += 1
    if not blocks:
        unknown += 1
    return blocks, unknown


def scan(text, behavior, fmt):
    types, sensitive, unknown, rows = collections.Counter(), collections.Counter(), 0, 0
    if '\0' in text or fmt not in ('yaml', 'text'):
        return types, sensitive, 1, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if fmt == 'yaml':
            if line == 'payload:' or line.startswith('payload: #'):
                continue
            if not line.startswith('- '):
                unknown += 1
                continue
            line = line[2:]
        try:
            rule = scalar(line)
        except (ValueError, TypeError):
            unknown += 1
            continue
        rows += 1
        if behavior in ('ipcidr', 'domain'):
            types[behavior] += 1
            continue
        if behavior != 'classical':
            unknown += 1
            continue
        kind = rule.split(',', 1)[0].strip()
        if not re.fullmatch('[A-Z][A-Z0-9-]*', kind):
            unknown += 1
            continue
        types[kind] += 1
        # Includes source conditions inside AND/OR/NOT, without printing values.
        for token in re.findall(r'(?:^|[,\s(])((?:SRC|PROCESS|IN)-[A-Z-]+|UID|DSCP)\s*,', rule):
            sensitive[token] += 1
        if re.search(r',\s*src\s*(?:,|$)', rule):
            sensitive['src-option'] += 1
        if re.search(r'(?:^|[,\s(])(?:RULE-SET|SUB-RULE)\s*,', rule):
            unknown += 1
    return types, sensitive, unknown, rows


def inspect(config, base):
    blocks, unknown = providers(config.read_text())
    result = {'providers': len(blocks), 'read_files': 0, 'missing_or_unsupported': 0,
              'unparsed_or_nested': unknown, 'rule_rows': 0,
              'behaviors': dict(collections.Counter(b.get('behavior', 'unknown') for b in blocks))}
    types, sensitive = collections.Counter(), collections.Counter()
    for b in blocks:
        try:
            path = base / b['path']
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('unreadable provider')
            t, s, u, rows = scan(path.read_text(), b.get('behavior'), b.get('format', 'yaml'))
            result['read_files'] += 1
            result['rule_rows'] += rows
            result['unparsed_or_nested'] += u + (rows == 0)
            types.update(t)
            sensitive.update(s)
        except (OSError, UnicodeError, KeyError, ValueError, TypeError):
            result['missing_or_unsupported'] += 1
    result.update(rule_types=dict(types), source_sensitive=dict(sensitive))
    return result


def main():
    try:
        # Match the running process, including its -d home for relative paths.
        for proc in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                args = proc.read_bytes().decode().split('\0')
                if Path(args[0]).name != 'clash' or '-f' not in args or '-d' not in args:
                    continue
                base = Path(args[args.index('-d') + 1])
                config = Path(args[args.index('-f') + 1])
                if not base.is_absolute() or not config.is_absolute():
                    continue
                result = inspect(config, base)
                print('RULE_FILES: ' + json.dumps(result, ensure_ascii=False, sort_keys=True))
                print('READ_ONLY: local files screened; live policy and route equivalence not certified.')
                return 0
            except (OSError, UnicodeError, IndexError):
                continue
        print('RULE_FILES: active Clash config unavailable')
        return 1
    except Exception as exc:
        print('RULE_FILES: check failed (' + type(exc).__name__ + ')')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
