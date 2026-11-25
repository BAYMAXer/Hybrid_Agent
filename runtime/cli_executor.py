"""Target-side CLI worker. No GUI input, host fallback, or success-by-stdout.

The worker uses stdlib only so the identical implementation can execute through
an OSWorld controller inside its guest. Shell commands are NOT a security sandbox.

Integration contract:
- A local adapter opts in with cli_target='local' and a prepared workspace.
- A guest adapter supplies cli_target='guest', guest_workspace_dir, and an
  explicit controller.run_python_script port. Transport failure never runs on
  the controller host; unknown dispatch is reported without automatic retry.
- Pipeline records the action, result, target, cwd, timing and review evidence;
  the sampled CLI action uses the same SFT/RL transition schema as DOM/GUI.
- File predicates are independently read on the target. Exit code zero is not
  a completion contract. Jev reviews remaining semantics using these facts.
- Windows shells use cmd.exe; Linux/macOS use bash. Relative paths use the run
  workspace unless cwd is explicit. Shell commands run with the runner's OS
  permissions. The workspace is a default location, not an access sandbox.
- stdout/stderr each expose at most 16 KB; directory listings expose 300 entries.
  Text reads are bounded, full-file equality uses streamed SHA-256 comparison.
- The native CLI-only runner supports Linux without a desktop/display server;
  Windows/macOS adapters also expose CLI alongside their existing UI modes.
"""
import base64
import inspect
import json

OPERATIONS = {'list', 'stat', 'read', 'write', 'mkdir', 'copy', 'move', 'rename', 'open', 'shell', 'compare'}


def validate_cli(args):
    if not isinstance(args, dict) or args.get('operation') not in OPERATIONS:
        return 'cli.operation 必须为 list/stat/read/write/mkdir/copy/move/rename/open/shell/compare'
    allowed = {'operation', 'path', 'destination', 'content', 'command', 'cwd', 'timeout_s', 'overwrite'}
    if set(args) - allowed:
        return '未知 CLI 参数：' + ', '.join(sorted(set(args) - allowed))
    required = {'shell': ['command'], 'write': ['path', 'content'],
                'compare': ['path', 'destination'], 'copy': ['path', 'destination'], 'move': ['path', 'destination'],
                'rename': ['path', 'destination']}.get(args['operation'], ['path'])
    for key in required:
        if not isinstance(args.get(key), str) or (key != 'content' and not args[key].strip()):
            return 'cli.' + key + ' 必须为字符串'
    for key in ('path', 'destination', 'cwd', 'command'):
        if key in args and (not isinstance(args[key], str) or '\x00' in args[key] or len(args[key]) > 16000):
            return '无效 CLI ' + key
    if len(args.get('content', '')) > 200000:
        return '单次写入不能超过 200000 字符'
    if 'overwrite' in args and type(args['overwrite']) is not bool:
        return 'overwrite 必须为布尔值'
    timeout = args.get('timeout_s', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        return 'timeout_s 必须在 (0,120] 秒内'
    return None


def office_contains_text(path, needle, timeout):
    """Search OOXML's logical document text without returning file contents."""
    import posixpath
    import time
    import zipfile
    from xml.etree import ElementTree as ET

    deadline = time.monotonic() + timeout
    remaining_bytes = 48 * 1024 * 1024

    def read_part(package, name):
        nonlocal remaining_bytes
        size = package.getinfo(name).file_size
        if time.monotonic() >= deadline or size > 8 * 1024 * 1024 or size > remaining_bytes:
            raise TimeoutError('Office text verification limit reached')
        remaining_bytes -= size
        data = package.read(name)
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('unsupported XML declaration')
        return ET.fromstring(data)

    def fragments(package):
        if path.suffix.casefold() == '.docx':
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            document = read_part(package, 'word/document.xml')
            body = document.find(ns + 'body')
            if document.tag != ns + 'document' or body is None:
                raise ValueError('invalid Word document body')
            for paragraph in body.iter(ns + 'p'):
                yield ''.join(child.text or '' for child in paragraph.iter(ns + 't'))
            return

        ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        relation_id = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        workbook = read_part(package, 'xl/workbook.xml')
        if workbook.tag != ns + 'workbook':
            raise ValueError('invalid Excel workbook')
        relationships = {item.get('Id'): item for item in read_part(package, 'xl/_rels/workbook.xml.rels')}

        def rich_text(element):
            return ''.join(child.text or '' for child in element.findall(ns + 't') + element.findall(ns + 'r/' + ns + 't'))

        shared_strings = []
        if 'xl/sharedStrings.xml' in package.namelist():
            shared_strings = [rich_text(item) for item in read_part(package, 'xl/sharedStrings.xml').iter(ns + 'si')]
        for sheet in workbook.iter(ns + 'sheet'):
            relationship = relationships.get(sheet.get(relation_id))
            if relationship is None or relationship.get('TargetMode') == 'External' or not relationship.get('Target'):
                raise ValueError('missing internal workbook sheet relationship')
            if not relationship.get('Type', '').endswith('/worksheet'):
                continue  # Charts and other non-worksheet workbooks parts carry no cells.
            target = relationship.get('Target')
            name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
            if not name.startswith('xl/'):
                raise ValueError('invalid worksheet relationship')
            worksheet = read_part(package, name)
            if worksheet.tag != ns + 'worksheet':
                raise ValueError('invalid Excel worksheet')
            for cell in worksheet.iter(ns + 'c'):
                kind = cell.get('t')
                value = cell.findtext(ns + 'v')
                if kind == 'inlineStr':
                    inline = cell.find(ns + 'is')
                    if inline is not None:
                        yield rich_text(inline)
                elif value is not None:
                    if kind == 's':
                        index = int(value)
                        if not 0 <= index < len(shared_strings):
                            raise ValueError('invalid shared string reference')
                        value = shared_strings[index]
                    elif kind == 'b':
                        value = {'0': 'FALSE', '1': 'TRUE'}.get(value, value)
                    yield value

    # Search the parsed part content, not the ZIP bytes or unused shared values.
    with zipfile.ZipFile(path) as package:
        read_part(package, '[Content_Types].xml')
        tail = ''
        for fragment in fragments(package):
            if time.monotonic() >= deadline:
                raise TimeoutError('Office text verification limit reached')
            content = tail + fragment
            if needle in content:
                return True
            tail = (content + '\n')[-len(needle) + 1:] if len(needle) > 1 else ''
    return False


def target_worker(args, workspace):
    # Serialized by execute_cli alongside the stdlib-only Office helper.
    import hashlib
    import os
    import shutil
    import signal
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    started = time.monotonic()
    result = {'channel': 'cli', 'operation': args['operation'], 'status': 'failed',
              'dispatched': False, 'stdout': '', 'stderr': '', 'exit_code': None}
    try:
        root = Path(workspace).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        cwd = Path(args.get('cwd') or root).expanduser()
        cwd = (root / cwd).resolve(strict=True) if not cwd.is_absolute() else cwd.resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError(str(cwd))
        result['cwd'] = str(cwd)
        def path(value):
            p = Path(value).expanduser()
            return (cwd / p).absolute() if not p.is_absolute() else p.absolute()
        def stat(p):
            exists = p.exists()
            item = {'path': str(p), 'exists': exists, 'is_file': p.is_file(), 'is_dir': p.is_dir()}
            if exists:
                s = p.stat()
                item.update(bytes=s.st_size, mtime_ns=s.st_mtime_ns)
            return item
        op = args['operation']
        result['dispatched'] = True
        if op == 'shell':
            command = args['command']
            executable = None
            if os.name == 'nt':
                import ctypes
                system_dir = ctypes.create_unicode_buffer(32768)
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.GetSystemDirectoryW.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
                kernel32.GetSystemDirectoryW.restype = ctypes.c_uint
                size = kernel32.GetSystemDirectoryW(system_dir, len(system_dir))
                if not 0 < size < len(system_dir):
                    raise OSError('无法定位 Windows 系统命令解释器')
                executable = os.path.join(system_dir.value, 'cmd.exe')
                # /s removes just our outer quotes, leaving command's own quotes
                # intact. Popen(list) would add C-runtime backslash escapes here.
                shell = f'"{executable}" /d /s /c "{command}"'
            else:
                shell = ['/bin/bash', '-c', command]
            result['command'] = command
            # Spool output, never buffer unbounded stdout in RAM or wait on
            # inherited pipes from a child launched by the command.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(shell, executable=executable, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, start_new_session=os.name != 'nt',
                                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                try:
                    proc.wait(timeout=args.get('timeout_s', 30))
                except subprocess.TimeoutExpired:
                    result['error_code'] = 'cli_timeout'
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except BaseException:
                    # User stop / runner termination must not leave a command
                    # behind just because it owns a separate process group.
                    if proc.poll() is None:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                    raise
                result['exit_code'] = proc.returncode
                for key, stream in [('stdout', out), ('stderr', err)]:
                    size = stream.tell()
                    stream.seek(0)
                    result[key] = stream.read(16000).decode('utf-8', errors='replace')
                    result[key + '_truncated'] = size > 16000
            result['status'] = 'executed' if result['exit_code'] == 0 and not result.get('error_code') else 'failed'
        else:
            p = path(args['path'])
            result['path'] = str(p)
            if op == 'stat':
                result['file'] = stat(p)
            elif op == 'list':
                entries = []
                with os.scandir(p) as scan:
                    for entry in scan:
                        if len(entries) >= 300:
                            result['truncated'] = True
                            break
                        entries.append(stat(Path(entry.path)))
                result['entries'] = sorted(entries, key=lambda e: e['path'])
            elif op == 'read':
                needle = args.get('_file_content')
                if needle and p.suffix.casefold() in {'.docx', '.xlsx'}:
                    before = p.stat()
                    found = office_contains_text(p, needle, args.get('timeout_s', 30))
                    after = p.stat()
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise OSError('file changed during Office text verification')
                    result.update(office_text_contains=found, content_scope='office_document_text', file=stat(p))
                else:
                    with p.open('rb') as f:
                        data = f.read(16001)
                    result.update(stdout=data[:16000].decode('utf-8', errors='replace'), stdout_truncated=len(data)>16000,
                                  file=stat(p))
            elif op == 'write':
                data = args['content'].encode('utf-8')
                # Exclusive create by default; replacement must be explicit.
                with p.open('wb' if args.get('overwrite') else 'xb') as f:
                    f.write(data)
                result['file'] = stat(p)
                readback = p.read_bytes()
                result['effect_verified'] = readback == data
                result['sha256'] = hashlib.sha256(data).hexdigest()
                result['content_preview'] = readback[:16000].decode('utf-8', errors='replace')
                result['content_truncated'] = len(readback) > 16000
            elif op == 'mkdir':
                p.mkdir(parents=True, exist_ok=True)
                result['file'] = stat(p)
                result['effect_verified'] = p.is_dir()
            elif op in {'copy', 'move', 'rename', 'compare'}:
                dest = path(args['destination'])
                result['destination'] = str(dest)
                if op != 'compare' and p.resolve() == dest.resolve():
                    raise ValueError('source and destination are the same')
                if not p.is_file():
                    raise ValueError('copy/move/rename requires a regular file')
                def digest(file):
                    h = hashlib.sha256()
                    with file.open('rb') as f:
                        for block in iter(lambda: f.read(1048576), b''):
                            h.update(block)
                    return h.hexdigest()
                original_hash = digest(p)
                if op == 'compare':
                    other_hash = digest(dest)
                    result.update(equal=original_hash == other_hash, file=stat(p), other_file=stat(dest), sha256=original_hash, other_sha256=other_hash)
                elif op in {'move', 'rename'}:
                    if args.get('overwrite'):
                        os.replace(p, dest)
                    else:
                        # Atomic exclusive destination; keeps inode/file ID so
                        # browser download receipts survive the rename.
                        try:
                            os.link(p, dest)
                        except OSError as exc:
                            import errno
                            if exc.errno not in {errno.EXDEV, errno.ENOTSUP}:
                                raise
                            with p.open('rb') as source, dest.open('xb') as target:
                                shutil.copyfileobj(source, target)
                            if digest(dest) != original_hash:
                                raise OSError('destination content mismatch; source retained')
                        p.unlink()
                else:
                    with p.open('rb') as source, dest.open('wb' if args.get('overwrite') else 'xb') as target:
                        shutil.copyfileobj(source, target)
                if op != 'compare':
                    if digest(dest) != original_hash:
                        raise OSError('destination content mismatch')
                    result.update(file=stat(dest), source=stat(p), sha256=original_hash, effect_verified=True)
            elif op == 'open':
                if not p.exists():
                    raise FileNotFoundError(str(p))
                if os.name == 'nt':
                    os.startfile(str(p))
                else:
                    import sys
                    subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(p)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                result['effect_verified'] = False  # Dispatch is not app readiness.
            result.update(status='executed', exit_code=0)
    except Exception as exc:
        result.update(status='failed', error_code=type(exc).__name__, stderr=str(exc)[:2000])
    result['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
    result['reason'] = ('后台操作已返回；子任务完成由独立核查决定' if result['status'] == 'executed'
                        else result.get('error_code') or '命令退出码非零')
    return result


def execute_cli(env, args, remaining_s=120, *, file_content=None):
    error = validate_cli(args)
    capability = cli_capability(env)
    if error or not capability['available'] or remaining_s <= 0:
        return {'channel': 'cli', 'status': 'rejected', 'dispatched': False,
                'reason': error or capability.get('reason') or '任务时限已耗尽'}
    args = dict(args, timeout_s=min(float(args.get('timeout_s', 30)), max(.001, remaining_s)))
    if file_content and args['operation'] == 'read':
        args['_file_content'] = file_content
    if capability['target'] == 'local':
        return target_worker(args, capability['workspace'])
    # Explicit guest Python port. NEVER call run_python/run_command wrappers:
    # their legacy host fallback can operate on the wrong computer.
    payload = base64.b64encode(json.dumps([args, capability['workspace']], ensure_ascii=False).encode()).decode()
    source = inspect.getsource(target_worker)
    if '_file_content' in args:
        source = inspect.getsource(office_contains_text) + '\n' + source
    code = source + '\nimport json,base64\nprint("AGENT_CLI_RESULT="+json.dumps(target_worker(*json.loads(base64.b64decode(' + repr(payload) + '))),ensure_ascii=False))'
    try:
        response = env.controller.run_python_script(code)
        output = response if isinstance(response, str) else response.get('output', '')
        line = next(line for line in reversed(output.splitlines()) if line.startswith('AGENT_CLI_RESULT='))
        result = json.loads(line.partition('=')[2])
        if result.get('channel') != 'cli':
            raise ValueError('invalid guest result')
        return result
    except Exception as exc:
        return {'channel': 'cli', 'status': 'uncertain', 'dispatched': True,
                'reason': '目标机 CLI 结果不可确认：' + type(exc).__name__}


def cli_capability(env):
    import os
    import sys
    initial = getattr(env, 'initial_state', None) or {}
    workspace = initial.get('workspace_dir') or initial.get('downloads_dir') or getattr(env, 'workspace_dir', None)
    local = getattr(env, 'cli_target', None) == 'local'
    guest = getattr(env, 'cli_target', None) == 'guest'
    if guest:
        workspace = getattr(env, 'guest_workspace_dir', None) or initial.get('target_workspace_dir')
        # Never interpret a controller-side download path as a guest path.
    available = bool(workspace and (local or guest and callable(getattr(getattr(env, 'controller', None), 'run_python_script', None))))
    target_os = getattr(env, 'os_name', '')
    capability = {'available': available, 'target': 'local' if local else 'guest', 'workspace': str(workspace or ''),
                  'os': target_os, 'cli_only': bool(getattr(env, 'cli_only', False)),
                  'reason': '' if available else '目标环境没有可用 CLI 执行器或默认工作目录'}
    if available and local:
        capability['shell'] = 'cmd.exe' if os.name == 'nt' else '/bin/bash'
        if sys.executable:
            capability['python_executable'] = os.path.abspath(sys.executable)
    elif available and guest:
        guest_os = str(target_os or '').lower()
        if guest_os in {'windows', 'win32', 'nt'}:
            capability['shell'] = 'cmd.exe'
        elif guest_os in {'linux', 'ubuntu', 'darwin', 'mac', 'macos'}:
            capability['shell'] = '/bin/bash'
    return capability


def file_check_samples(env, predicates, remaining):
    samples = []
    for p in predicates:
        if p.get('kind') not in {'file_exists', 'file_absent', 'file_content', 'files_equal'}:
            continue
        args = {'operation': 'read' if p['kind'] == 'file_content' else 'stat', 'path': p['path']}
        if p['kind'] == 'files_equal':
            args.update(operation='compare', destination=p['other_path'])
        expected = p.get('text') if p['kind'] == 'file_content' else None
        samples.append({'predicate': dict(p), 'result': execute_cli(env, args, remaining_s=remaining(), file_content=expected)})
    return samples
