"""Explicit miner lifecycle, state persistence and redacted bounded logging."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from types import SimpleNamespace

from constants import DATA_DIR, PriorityMode, ClientType
from settings import Settings
from translate import _
from utils import json_save
from webui.bridge import WebBridge


class ServiceHandler(logging.Handler):
    def __init__(self, service):
        super().__init__(logging.INFO)
        self.service = service

    def emit(self, record):
        self.service.log(record.levelname, self.format(record))


class MinerService:
    def __init__(self, data_dir=DATA_DIR, worker_factory=None, password=''):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.intent_path = self.data_dir / 'web-state.json'
        self.worker_factory = worker_factory
        self.password = password
        self.logs = deque(maxlen=500)
        self.sequence = 0
        self.client = self.task = None
        self.phase = 'stopped'
        self.error = None
        self.started_at = None
        self.lock = asyncio.Lock()
        self.autostart = False
        if self.intent_path.exists():
            self.autostart = json.loads(self.intent_path.read_text()).get('autostart', False) is True
        args = SimpleNamespace(log=False, tray=False, dump=False, logging_level=logging.INFO,
                               debug_ws=logging.WARNING, debug_gql=logging.WARNING)
        self.settings = Settings(args)
        self._set_language()
        log_dir = self.data_dir / 'web-logs'
        log_dir.mkdir(mode=0o700, exist_ok=True)
        self.file_handler = RotatingFileHandler(log_dir / 'application.log', maxBytes=2*1024*1024,
                                               backupCount=3, encoding='utf-8')
        self.file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        self.handler = ServiceHandler(self)
        self.handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
        self.logger = logging.getLogger('TwitchDrops')
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        # Upstream debug requests can contain headers/tokens; never enable them here.
        logging.getLogger('TwitchDrops.gql').setLevel(logging.WARNING)
        logging.getLogger('TwitchDrops.websocket').setLevel(logging.WARNING)

    def _set_language(self):
        with suppress(ValueError):
            _.set_language(self.settings.language)

    def redact(self, text):
        text = str(text)
        secrets = [self.password]
        if self.client:
            auth = self.client._auth_state
            secrets.append(getattr(auth, 'access_token', ''))
            if self.client.gui.login.device:
                secrets.append(self.client.gui.login.device['code'])
        for secret in secrets:
            if secret:
                text = text.replace(secret, '<redacted>')
        text = re.sub(r'(?i)(OAuth|Bearer)\s+[^\s\"\',;]+', r'\1 <redacted>', text)
        text = re.sub(r'(?i)((?:password|access_token|refresh_token|device_code|user_code|auth-token|cookie|authorization)[\"\s:=]+)[^\s,;]+', r'\1<redacted>', text)
        text = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?<redacted>', text)
        return text[:16000]

    def log(self, level, message):
        message = self.redact(message)
        self.sequence += 1
        self.logs.append({'id': self.sequence, 'at': datetime.now(timezone.utc).isoformat(),
                          'level': level, 'message': message})
        record = logging.LogRecord('miner', getattr(logging, level, logging.INFO), '', 0, message, (), None)
        self.file_handler.emit(record)
        # Emit the same redacted message to Docker stdout, not just to the browser.
        print(f'{level} {message}', flush=True)

    def _save_intent(self, enabled):
        json_save(self.intent_path, {'autostart': enabled})
        self.autostart = enabled

    async def start(self):
        async with self.lock:
            if self.task and not self.task.done():
                return
            if self.worker_factory is None:
                from twitch import Twitch
                # This is an explicit web-service default, not a retry/fallback loop.
                factory = lambda settings, **kwargs: Twitch(settings, client_type=ClientType.SMARTBOX, **kwargs)
            else:
                factory = self.worker_factory
            self.error = None
            self.client = factory(self.settings, gui_factory=lambda twitch: WebBridge(twitch, self))
            self._save_intent(True)
            self.phase = 'starting'
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.task = asyncio.create_task(self._run(self.client), name='miner-service')
            self.log('INFO', '后台服务已启动')
            if self.worker_factory is None:
                self.log('INFO', '设备授权客户端：SMARTBOX')

    async def _run(self, client):
        self.phase = 'running'
        try:
            await client.run()
            if not client.gui.close_requested:
                self.error = '核心任务提前结束，请检查日志后手动重试'
                self.phase = 'error'
                self.log('ERROR', self.error)
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            # Captcha and API failures are reported, not hidden by restart loops.
            self.error = self.redact(traceback.format_exc())
            self.phase = 'error'
            self.log('ERROR', self.error)
        finally:
            client.gui.login.device = None
            try:
                await asyncio.wait_for(client.shutdown(), timeout=20)
            except Exception as exc:
                self.log('ERROR', f'关闭连接失败: {type(exc).__name__}')
            try:
                client.save(force=True)
            except Exception:
                import traceback
                self.error = self.redact(traceback.format_exc())
                self.phase = 'error'
                self.log('ERROR', self.error)
            if self.phase != 'error':
                self.phase = 'stopped'

    async def stop(self, *, preserve_intent=False):
        async with self.lock:
            if not preserve_intent:
                self._save_intent(False)
            if self.task and not self.task.done():
                self.phase = 'stopping'
                self.client.gui.close()
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), timeout=100)
                except asyncio.TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
                    self.log('ERROR', '后台停止超时，已取消未完成任务')
            if self.phase != 'error':
                self.phase = 'stopped'
            if self.client:
                self.client.gui.login.device = None

    async def reload(self):
        await self.stop(preserve_intent=True)
        await self.start()

    async def close(self):
        await self.stop(preserve_intent=True)
        self.logger.removeHandler(self.handler)
        self.file_handler.close()

    def settings_view(self):
        return {'priority': self.settings.priority, 'exclude': sorted(self.settings.exclude),
                'priority_mode': self.settings.priority_mode.name,
                'enable_badges_emotes': self.settings.enable_badges_emotes,
                'available_drops_check': self.settings.available_drops_check,
                'connection_quality': self.settings.connection_quality,
                'language': self.settings.language}

    def update_settings(self, data):
        allowed = set(self.settings_view())
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError('存在不支持的设置字段')
        # Validate everything before mutating or persisting any setting.
        clean = {}
        for key, value in data.items():
            if key in ('priority', 'exclude'):
                if not isinstance(value, list) or len(value) > 300 or any(
                    not isinstance(v, str) or not 1 <= len(v.strip()) <= 200 for v in value):
                    raise ValueError('游戏列表格式错误')
                value = list(dict.fromkeys(v.strip() for v in value))
                if key == 'exclude':
                    value = set(value)
            elif key == 'priority_mode':
                if not isinstance(value, str) or value not in PriorityMode.__members__:
                    raise ValueError('优先模式无效')
                value = PriorityMode[value]
            elif key in ('enable_badges_emotes', 'available_drops_check'):
                if type(value) is not bool:
                    raise ValueError('布尔设置无效')
            elif key == 'connection_quality':
                if type(value) is not int or not 1 <= value <= 6:
                    raise ValueError('连接质量必须为 1–6')
            elif key == 'language':
                if value not in list(_.languages):
                    raise ValueError('语言不可用')
            clean[key] = value
        for key, value in clean.items():
            setattr(self.settings, key, value)
        self.settings.save(force=True)
        self._set_language()
        self.log('INFO', '设置已保存；运行中的任务请点击重新加载以应用全部更改')

    def snapshot(self):
        model = self.client.gui.snapshot() if self.client else {
            'status': '等待启动', 'account': {'status': '未登录', 'user_id': None, 'device': None, 'authenticated': False},
            'games': [], 'campaigns': [], 'channels': [], 'watching': None, 'drop': None,
            'websockets': []}
        return {**model, 'phase': self.phase, 'error': self.error, 'started_at': self.started_at,
                'autostart': self.autostart, 'settings': self.settings_view(),
                'languages': list(_.languages), 'logs': list(self.logs)}
