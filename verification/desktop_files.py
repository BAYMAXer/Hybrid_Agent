"""Read-only verification of saved files in the task's target workspace."""
import base64
import inspect
import json
import os
import re

from ..runtime.cli_executor import cli_capability


FILE_PREDICATES = frozenset({'file_exists', 'file_absent', 'file_content', 'files_equal'})


def normalize_desktop_file(check):
    if (not isinstance(check.get('path'), str) or not check['path'].strip()
            or '\x00' in check['path'] or len(check['path']) > 1024):
        raise ValueError('desktop_file requires a nonempty workspace path')
    if check.get('scope') != 'final':
        raise ValueError('desktop_file must be revalidated at completion')
    check['change'] = check.get('change', 'present')
    if check['change'] not in ('present', 'created', 'modified'):
        raise ValueError('desktop_file.change must be present, created or modified')
    if check.get('format') not in (None, 'txt', 'csv', 'docx', 'xlsx'):
        raise ValueError('desktop_file.format must be txt, csv, docx or xlsx')
    for key in ('text_equals', 'text_contains'):
        if key in check and (not isinstance(check[key], str) or len(check[key]) > 16000):
            raise ValueError(f'desktop_file.{key} must be text up to 16000 characters')
        if key in check and check.get('format') == 'xlsx':
            raise ValueError('desktop_file spreadsheet content must use cells')
    if 'text_contains' in check and not check['text_contains']:
        raise ValueError('desktop_file.text_contains must be nonempty')
    if 'sha256' in check:
        if not isinstance(check['sha256'], str) or not re.fullmatch('[0-9a-fA-F]{64}', check['sha256']):
            raise ValueError('desktop_file.sha256 must contain 64 hexadecimal characters')
        check['sha256'] = check['sha256'].lower()
    if 'cells' in check:
        cells = check['cells']
        if (check.get('format') != 'xlsx' or not isinstance(cells, list) or not 1 <= len(cells) <= 32
                or any(not isinstance(c, dict) or set(c) not in ({'sheet', 'cell', 'value'}, {'sheet', 'cell', 'formula'})
                       or not all(isinstance(v, str) and len(v) <= 1024 for v in c.values())
                       or not c['sheet'] or not re.fullmatch('[A-Z]{1,3}[1-9][0-9]{0,6}', c['cell']) for c in cells)):
            raise ValueError('desktop_file.cells requires XLSX and {sheet, cell, value|formula} records')


def desktop_file_probe(workspace, check):
    """Run unchanged on the target machine. Return facts without file content."""
    import hashlib
    import os
    import posixpath
    import stat
    import zipfile
    from pathlib import Path
    from xml.etree import ElementTree as ET

    def identity(s):
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    def part(package, name):
        if package.getinfo(name).file_size > 8 * 1024 * 1024:
            raise OverflowError('document_part_too_large')
        data = package.read(name)
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('unsupported_xml_declaration')
        return ET.fromstring(data)

    try:
        proposed = Path(check['path']).expanduser()
        if proposed.is_absolute():
            # This path comes from a trusted explicit completion contract.
            path = proposed.resolve(strict=False)
        else:
            root = Path(workspace).expanduser().resolve(strict=True)
            path = (root / proposed).resolve(strict=False)
            if path == root or not path.is_relative_to(root):
                return {'status': 'unknown', 'reason': 'relative output path escapes the target workspace'}
        if os.name == 'nt' and any(':' in p for p in path.parts[1:]):
            return {'status': 'unknown', 'reason': 'Windows alternate data streams are not desktop files'}
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return {'status': 'invalid', 'reason': 'path is not a regular file'}
        if before.st_size > 128 * 1024 * 1024:
            return {'status': 'unknown', 'reason': 'file exceeds the verification byte limit'}
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (os.name != 'nt' and identity(opened) != identity(before)):
                return {'status': 'unknown', 'reason': 'file changed before verification'}
            digest = hashlib.sha256()
            total = 0
            while data := handle.read(1024 * 1024):
                total += len(data)
                if total > 128 * 1024 * 1024:
                    return {'status': 'unknown', 'reason': 'file grew beyond the verification byte limit'}
                digest.update(data)
            result = {'status': 'read', 'path': str(path), 'bytes': total, 'sha256': digest.hexdigest(),
                      'file_identity': list(identity(before))}
            handle.seek(0)
            fmt = check.get('format')
            text = None
            try:
                if fmt in ('docx', 'xlsx'):
                    with zipfile.ZipFile(handle) as package:
                        part(package, '[Content_Types].xml')
                        if fmt == 'docx':
                            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                            document = part(package, 'word/document.xml')
                            if document.tag != ns + 'document':
                                raise ValueError('invalid_document_part')
                            text = '\n'.join(''.join(t.text or '' for t in p.iter(ns + 't')) for p in document.iter(ns + 'p'))
                        else:
                            ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                            book = part(package, 'xl/workbook.xml')
                            if book.tag != ns + 'workbook':
                                raise ValueError('invalid_workbook_part')
                            if check.get('cells'):
                                relid = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
                                links = {el.get('Id'): el.get('Target', '') for el in part(package, 'xl/_rels/workbook.xml.rels')}
                                sheets = {el.get('name'): links.get(el.get(relid), '') for el in book.iter(ns + 'sheet')}
                                shared = ([''.join(t.text or '' for t in item.iter(ns + 't')) for item in part(package, 'xl/sharedStrings.xml')]
                                          if 'xl/sharedStrings.xml' in package.namelist() else [])
                                actual = {}
                                for name in {c['sheet'] for c in check['cells']} & sheets.keys():
                                    target = sheets[name]
                                    name_in_package = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                                    if not name_in_package.startswith('xl/'):
                                        raise ValueError('invalid_sheet_relationship')
                                    actual[name] = {el.get('r'): el for el in part(package, name_in_package).iter(ns + 'c')}
                                matches = []
                                for expected in check['cells']:
                                    element = actual.get(expected['sheet'], {}).get(expected['cell'])
                                    if element is None:
                                        matches.append(False)
                                    elif 'formula' in expected:
                                        matches.append((element.findtext(ns + 'f') or '') == expected['formula'].removeprefix('='))
                                    else:
                                        value = element.findtext(ns + 'v') or ''
                                        if element.get('t') == 's':
                                            value = shared[int(value)]
                                        elif element.get('t') == 'inlineStr':
                                            value = ''.join(t.text or '' for t in element.iter(ns + 't'))
                                        matches.append(value == expected['value'])
                                result['cells_match'] = all(matches)
                elif fmt or 'text_equals' in check or 'text_contains' in check:
                    data = handle.read(2 * 1024 * 1024 + 1)
                    if len(data) > 2 * 1024 * 1024:
                        raise OverflowError('text_too_large')
                    text = data.decode('utf-8-sig')
                if text is not None:
                    text = text.replace('\r\n', '\n')
                    if 'text_equals' in check:
                        result['text_equals'] = text == check['text_equals'].replace('\r\n', '\n')
                    if 'text_contains' in check:
                        result['text_contains'] = check['text_contains'].replace('\r\n', '\n') in text
                if fmt:
                    result['format'] = fmt
            except (KeyError, IndexError, UnicodeError, ValueError, zipfile.BadZipFile, ET.ParseError):
                return {'status': 'invalid', 'reason': 'file does not match the required format'}
            if identity(os.fstat(handle.fileno())) != identity(opened) or identity(path.stat()) != identity(before):
                return {'status': 'unknown', 'reason': 'file changed during verification'}
            if os.name == 'nt':
                # Windows can use distinct information fields/rounding for a path
                # and an open descriptor. Compare each kind to its own baseline.
                with os.fdopen(os.open(path, flags), 'rb') as current:
                    if identity(os.fstat(current.fileno())) != identity(opened) or identity(path.stat()) != identity(before):
                        return {'status': 'unknown', 'reason': 'file was replaced during verification'}
            return result
    except FileNotFoundError:
        return {'status': 'missing', 'reason': 'expected file is absent'}
    except (OSError, RuntimeError, OverflowError):
        return {'status': 'unknown', 'reason': 'target file could not be read within verification limits'}


def sample_desktop_file(env, check):
    capability = cli_capability(env)
    if not capability['available']:
        return {'status': 'unknown', 'reason': 'no target workspace reader is available'}
    workspace = capability['workspace']
    if capability['target'] == 'local':
        return desktop_file_probe(workspace, check)
    payload = base64.b64encode(json.dumps([workspace, check], ensure_ascii=False).encode()).decode('ascii')
    script = (inspect.getsource(desktop_file_probe) + '\nimport json,base64\n'
              'print("AGENT_DESKTOP_FILE=" + json.dumps(desktop_file_probe(*json.loads(base64.b64decode('
              + repr(payload) + '))), ensure_ascii=False))')
    try:
        response = env.controller.run_python_script(script)
        output = response if isinstance(response, str) else response['output']
        line = next(line for line in reversed(output.splitlines()) if line.startswith('AGENT_DESKTOP_FILE='))
        facts = json.loads(line.partition('=')[2])
        if not isinstance(facts, dict) or facts.get('status') not in {'read', 'missing', 'invalid', 'unknown'}:
            raise ValueError('invalid file observation')
        if facts['status'] == 'read' and not re.fullmatch('[0-9a-f]{64}', str(facts.get('sha256', ''))):
            raise ValueError('file observation lacks a hash')
        return facts
    except (AttributeError, KeyError, OSError, RuntimeError, StopIteration, TypeError, ValueError):
        return {'status': 'unknown', 'reason': 'target workspace file reader did not return evidence'}


def check_desktop_file(env, check, baseline):
    current = sample_desktop_file(env, check)
    status = current['status']
    if status != 'read':
        return 'failed' if status in {'missing', 'invalid'} else 'unknown', current.get('reason', 'file evidence unavailable'), current
    change = check['change']
    if change != 'present':
        previous = (baseline or {}).get('status')
        if previous not in {'read', 'missing'}:
            return 'unknown', 'initial target file state is unavailable', current
        if change == 'created' and previous != 'missing':
            return 'failed', 'target file already existed before the task', current
        if change == 'modified' and (previous != 'read' or baseline['sha256'] == current['sha256']):
            return 'failed', 'target file content was not modified from the initial state', current
    if check.get('sha256', current['sha256']) != current['sha256'] or any(current.get(key) is False for key in ('text_equals', 'text_contains', 'cells_match')):
        return 'failed', 'saved file content differs from the required content', current
    return 'passed', 'saved file independently read on the target computer', current


def durable_file_predicates(steps, relocations, workspace=''):
    """Carry persistent leaves through proven moves and later removals."""
    def absolute(path):
        return os.path.abspath(os.path.join(workspace, path))

    def key(path):
        return os.path.normcase(absolute(path))

    def relocated(path):
        current = key(path)
        visited = set()
        while current in moves and current not in visited:
            visited.add(current)
            path = moves[current]
            current = key(path)
        return path

    moves = {key(src): absolute(dst) for src, dst in relocations.items()} if workspace else {}
    selected = []
    for step in steps:
        final = step.get('phase') in {'verify', 'final'}
        if step.get('phase') in {'temporary', 'intermediate'} and not final:
            continue
        for original in step.get('verification', []):
            if original.get('kind') not in FILE_PREDICATES or original.get('scope') == 'milestone':
                continue
            predicate = dict(original)
            kind = predicate['kind']
            if kind != 'file_absent' and not final and predicate.get('scope') != 'final':
                for field in ('path', 'other_path'):
                    if field in predicate:
                        predicate[field] = relocated(predicate[field])
            terminal = final or predicate.get('scope') == 'final'
            target = key(predicate['path'])
            if kind == 'file_absent':
                selected = [(p, explicit) for p, explicit in selected if explicit or not any(
                    field in p and key(p[field]) == target for field in ('path', 'other_path'))]
            else:
                targets = {key(predicate[field]) for field in ('path', 'other_path') if field in predicate}
                selected = [(p, explicit) for p, explicit in selected
                            if explicit or p['kind'] != 'file_absent' or key(p['path']) not in targets]
            if not any(p == predicate for p, _ in selected):
                selected.append((predicate, terminal))
    return [p for p, _ in selected]
