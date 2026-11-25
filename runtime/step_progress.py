"""Bounded progress monitoring for a plan step, independent of action spelling.

Screenshots and operator self-reports alone cannot reset this monitor.
Independent visual-effect judgments receive only bounded exploration credit. This is a
recovery signal, never a completion verdict or training reward.
"""
from dataclasses import dataclass
import json
import ntpath
import re

from ..desktop.desktop_readiness import launch_target
from ..desktop.desktop_state import windows_application_process
from ..desktop.explorer_location import committed_explorer_path, filesystem_path, ready_explorer


@dataclass(frozen=True)
class ProgressConfig:
    no_progress_limit: int = 4
    exploration_allowance: int = 2

    def __post_init__(self):
        if type(self.no_progress_limit) is not int or self.no_progress_limit < 2:
            raise ValueError("progress_config.no_progress_limit must be an integer >= 2")
        if type(self.exploration_allowance) is not int or not 0 <= self.exploration_allowance <= 4:
            raise ValueError("progress_config.exploration_allowance must be an integer in [0, 4]")


def _nodes(obs):
    return [n for b in (getattr(obs, 'context', None) or []) if b.get('kind') in {'browser_ax', 'desktop_ax'}
            for n in b.get('nodes', [])]


def _url(obs):
    return next((b.get('url', '') for b in (getattr(obs, 'context', None) or [])
                 if b.get('kind') == 'browser_ax'), '')


def _controls(obs):
    result = {}
    for n in _nodes(obs):
        if n.get('role') not in {'textbox', 'searchbox', 'combobox', 'spinbutton', 'checkbox', 'radio'}:
            continue
        key = (n.get('role'), n.get('name', ''))
        value = {'value': n.get('value'), **{k: (n.get('states') or {}).get(k, n.get(k))
                 for k in ('checked', 'selected')}}
        # Duplicate accessible names are ambiguous; do not claim verified input.
        result[key] = None if key in result else value
    return result


def _task_names_path(task, path):
    # Allow adjacent Chinese instructions ("D:\\Download创建..."), without
    # treating a common prefix ("D:\\Down", "D:\\Download42") as a match.
    boundary = r"""(?=$|[\s"'“”‘’，。；、,;!?()（）]|[.](?:\s|$)|(?:目录|文件夹)?(?:中|下|内)?(?:创建|新建|保存|打开|输入|并|复制|移动))"""
    return bool(path and re.search(re.escape(path) + boundary, (task or '').replace('/', '\\'), re.I))


def _is_target_folder(folder, task, public_checks, step):
    if not folder:
        return False
    if _task_names_path(task, folder):
        return True
    for checks, trusted in ((public_checks, True), ((step or {}).get('verification', ()), False)):
        for check in checks or ():
            kind = check.get('kind')
            if kind not in {'desktop_file', 'file_exists', 'file_absent', 'file_content', 'files_equal', 'explorer_folder_equals'}:
                continue
            for field in ('path', 'other_path'):
                path = filesystem_path(check.get(field))
                directory = path if kind == 'explorer_folder_equals' or check.get('file_type') == 'directory' else ntpath.dirname(path)
                if ntpath.normcase(directory) == ntpath.normcase(folder) and (trusted or _task_names_path(task, path)):
                    return True
    return False


class StepProgressMonitor:
    def __init__(self, config=None):
        self.config = config or ProgressConfig()
        self.step_id = None
        self.idle = self.decisions = self.explorations = 0
        self.seen = set()
        self.attempts = []
        self.collected_seen = set()

    def _fresh(self, key):
        key = json.dumps(key, sort_keys=True, ensure_ascii=False)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def _explorer_progress(self, before, after, action, task, public_checks, step, evidence):
        new_window = ready_explorer(after)
        if not action or not new_window or action.action in {'wait', 'observe_desktop'}:
            return ''
        old_window = ready_explorer(before)
        opening = launch_target(action) or (action.action == 'switch_applications'
                                             and windows_application_process(action.args.get('app_code')) == 'explorer')
        if (opening and not old_window and self.explorations < self.config.exploration_allowance
                and self._fresh(['explorer_ready'])):
            self.explorations += 1
            evidence.append('资源管理器首次在前台就绪，可开始导航（有界探索宽限）')
        old_folder, folder = committed_explorer_path(before), committed_explorer_path(after)
        changed = ntpath.normcase(old_folder) != ntpath.normcase(folder)
        if _is_target_folder(folder, task, public_checks, step):
            if changed and self._fresh(['explorer_folder', ntpath.normcase(folder)]):
                evidence.append('资源管理器活动视图已进入任务指定目录：' + folder)
            # Once there, choosing "New" or a menu item can legitimately advance
            # the task without any further directory transition.
            return ''
        if action.action != 'click' or changed or not old_window or old_window['hwnd'] != new_window['hwnd']:
            return ''
        target = str(action.args.get('target') or '').strip()
        folder_item = (re.search(r'导航栏|导航窗格|驱动器|文件夹图标|\([a-z]:\)|folder icon|navigation pane', target, re.I)
                       or bool(target and ('\\' + target.casefold()) in (task or '').casefold().replace('/', '\\')))
        if not folder_item:
            return ''
        feedback = f'单击后目录未切换，当前仍为 {folder}。' if folder else '单击后尚未核实目录切换。'
        return feedback + '要进入主区的文件夹可双击；已知绝对路径可在地址栏输入并按 Enter。'

    def observe(self, step, review, before, after, action, info, *, collected_sources=(), task='', public_checks=()):
        step_id = review.get('plan_step_id') or (step or {}).get('id', 'final')
        if step_id != self.step_id:
            self.step_id = step_id
            self.idle = self.decisions = self.explorations = 0
            self.seen.clear()
            self.attempts.clear()
            self._fresh(['url', _url(before)])
            self._fresh(['explorer_folder', ntpath.normcase(committed_explorer_path(before))])
            if ready_explorer(before):
                self._fresh(['explorer_ready'])
            for key, value in _controls(before).items():
                self._fresh(['control', key, value])
            for artifact in (getattr(before, 'info', None) or {}).get('artifacts', []):
                if artifact.get('completed') is True:
                    self._fresh(['artifact', artifact.get('artifact_id') or artifact.get('download_id') or artifact.get('path')])
        status, evidence = 'watching', []
        navigation_feedback = ''
        pending = (info.get('pending_downloads') or (getattr(after, 'info', None) or {}).get('pending_downloads')
                   or info.get('status') in {'pending', 'in_progress'})
        if pending:
            status = 'waiting'
        elif review.get('status') == 'passed':
            self.idle = 0
            status, evidence = 'advanced', ['当前步骤已通过独立审查']
        else:
            self.decisions += 1
            name = getattr(action, 'action', 'parse_failure')
            args = getattr(action, 'args', {})
            self.attempts.append({'action': name, 'target': str(args.get('target', args.get('key', '')))[:160],
                                  'status': info.get('status', 'unknown')})
            self.attempts[:] = self.attempts[-8:]
            for index, check in enumerate(review.get('code_checks', review.get('checks', []))):
                if check.get('status') == 'passed' and self._fresh(['check', index, check.get('kind'), check.get('id')]):
                    evidence.append('当前步骤的一个代码检查项已满足')
            moved = (getattr(after, 'info', None) or {}).get('desktop_window_moved')
            successful = not moved and not info.get('error_code') and info.get('status') == 'executed'
            if successful:
                if info.get('dispatched') is not False:
                    navigation_feedback = self._explorer_progress(before, after, action, task, public_checks, step, evidence)
                old_controls = _controls(before)
                if name in {'type', 'press', 'hotkey', 'select', 'click'}:
                    for key, value in _controls(after).items():
                        if value is None or key not in old_controls or old_controls[key] is None or value == old_controls[key]:
                            continue
                        if navigation_feedback and value.get('selected') != old_controls[key].get('selected'):
                            continue
                        if name == 'type' and str(args.get('text', '')) not in str(value.get('value') or ''):
                            continue
                        if self._fresh(['control', key, value]):
                            evidence.append('可访问性树确认了新的输入值或选择状态')
                artifacts = [*info.get('artifacts', []), *(getattr(after, 'info', None) or {}).get('artifacts', [])]
                for artifact in artifacts:
                    ident = artifact.get('artifact_id') or artifact.get('download_id') or artifact.get('path')
                    if ident and artifact.get('completed') is True and self._fresh(['artifact', ident]):
                        evidence.append('系统观察到新的已完成下载；内容仍须通过验收')
                # Navigation/scrolling can be useful exploration, but cannot
                # excuse endless alternating pages or unrelated visual changes.
                novel_page = _url(after) and _url(after) != _url(before) and self._fresh(['url', _url(after)])
                old_names = {(n.get('role'), n.get('name')) for n in _nodes(before)}
                new_names = sorted((str(n.get('role')), str(n.get('name'))) for n in _nodes(after)
                                   if n.get('name') and (n.get('role'), n.get('name')) not in old_names)
                discovery = name == 'scroll' and new_names and self._fresh(['discovery', new_names])
                if (novel_page or discovery) and self.explorations < self.config.exploration_allowance:
                    self.explorations += 1
                    evidence.append('发现新页面或滚动后新的结构内容（有界探索宽限）')
            # Information gathering has durable page evidence, unlike arbitrary
            # navigation. Require an independent progress judgment AND novel
            # non-link text retained by the framework. Never grants completion.
            if (successful and collected_sources and review.get('step_outcome') == 'progress'
                    and not review.get('progress_hint')):
                current = next((s for s in collected_sources if s.get('url') == _url(after)), None)
                parents = {str(n.get('ref')): n for n in _nodes(after)}
                facts = {str(n.get('name', '')).strip() for n in _nodes(after)
                         if n.get('role') in {'StaticText', 'paragraph', 'cell'}
                         and parents.get(str(n.get('parent')), {}).get('role') != 'link'
                         and len(str(n.get('name', '')).strip()) >= 6}
                new_facts = facts - self.collected_seen
                if current and new_facts and sum(map(len, new_facts)) >= 12:
                    self.collected_seen.update(facts)
                    evidence.append('已保存新的网页正文证据，且独立核查确认采集有进展；尚未判定完成')
            effect = review.get('action_effect', {})
            fingerprint = effect.get('evidence_fingerprint')
            if (successful and not navigation_feedback and effect.get('method') == 'decision_model' and effect.get('status') == 'passed'
                    and fingerprint and self.explorations < self.config.exploration_allowance
                    and self._fresh(['judge_effect', fingerprint])):
                self.explorations += 1
                evidence.append('独立视觉核查认为动作取得进展（有界宽限，不作为完成证据）')
            self.idle = 0 if evidence else self.idle + 1
            if self.idle >= self.config.no_progress_limit:
                status = 'stalled'
        reason = {'waiting': '系统操作仍在进行，暂停子任务无进展计数',
                  'advanced': '当前步骤已核查通过',
                  'stalled': '当前子任务连续多次没有新的可核实进展，需修订执行路径',
                  'watching': '已观察到进展' if evidence else '尚未观察到当前子任务的新进展'}[status]
        return {'plan_step_id': step_id, 'goal': (step or {}).get('goal', review.get('goal', '')),
                'status': status, 'reason': reason, 'evidence': evidence,
                **({'navigation_feedback': navigation_feedback} if navigation_feedback else {}),
                'idle_decisions': self.idle, 'step_decisions': self.decisions,
                'limit': self.config.no_progress_limit, 'recent_attempts': list(self.attempts),
                'source': 'framework_progress_monitor', 'policy_trainable': False}
