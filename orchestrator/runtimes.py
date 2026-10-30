"""Runtime adapters for the complete prepare → chat → play lifecycle."""
from dataclasses import dataclass
import json
import os
import re

from .config import REPO_ROOT, RunConfig, load_provider, load_api_key, load_dotenv
from .efforts import normalize_efforts
from .execution import workspace_capabilities


@dataclass(frozen=True)
class Runtime:
    engine: str
    label: str
    fast: bool = False
    mcp: bool = True
    telemetry_source: str = 'claude_stream_json'
    default_model: str = 'opus'
    default_effort: str = 'high'
    default_knowledge: str = 'full'

    def workspace(self, cfg):
        if cfg.engine != self.engine:
            raise ValueError('执行器与配置不一致')
        return workspace_capabilities(cfg)

    def providers(self):
        if self.engine == 'codex':
            return [{'id':'codex','label':self.label}]
        names = set()
        for path in (REPO_ROOT / 'configs/providers.json', REPO_ROOT / 'configs/local.providers.json'):
            if path.exists():
                names.update(k for k,v in json.loads(path.read_text(encoding='utf-8-sig')).items()
                             if not k.startswith('_') and isinstance(v,dict))
        return [{'id':name,'label':load_provider(name).get('label',name)} for name in sorted(names)]

    def configure(self, cfg, provider=None):
        cfg.engine = self.engine
        if self.engine == 'codex':
            cfg.runtime = 'container'  # Keep legacy --runtime host as an input alias only.
        cfg.provider = 'codex' if self.engine == 'codex' else provider or 'anthropic'
        if not isinstance(cfg.provider, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,60}', cfg.provider):
            raise ValueError('提供方标识无效')
        try:
            cfg.provider_info = ({'label':self.label, 'images':True,
                                  'allowed_domains':['chatgpt.com', 'auth.openai.com', 'api.openai.com']}
                                 if self.engine == 'codex' else load_provider(cfg.provider))
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        return cfg

    def token(self, cfg):
        if self.engine == 'codex':
            return ''
        if cfg.third_party:
            try:
                return load_api_key(cfg.provider_info)
            except SystemExit as exc:
                raise ValueError(str(exc)) from exc
        token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') or load_dotenv(REPO_ROOT / '.env').get('CLAUDE_CODE_OAUTH_TOKEN')
        if not token:
            raise ValueError('缺少 CLAUDE_CODE_OAUTH_TOKEN，请先配置 Claude 登录令牌。')
        return token

    def models(self, provider=None, *, refresh=False):
        if self.engine == 'codex':
            from .model_catalog import load_models
            return load_models(refresh=refresh)
        try:
            info = load_provider(provider or 'anthropic')
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        rows = info.get('models') or []
        models = []
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', str(row.get('id', ''))):
                continue
            efforts = normalize_efforts(row.get('efforts'))
            if efforts:
                models.append({'id':row['id'], 'label':row.get('label', row['id']), 'description':row.get('description',''),
                               'efforts':efforts, 'default_effort':row.get('default_effort') if row.get('default_effort') in efforts
                               else self.default_effort if self.default_effort in efforts else efforts[0]})
        if not models:
            raise ValueError('该提供方未配置模型列表，请在 configs/local.providers.json 的 models 中添加模型。')
        return {'models':models, 'default_model':models[0]['id'], 'source':'provider_config'}

    def resolve(self, model, effort, provider=None):
        if self.engine == 'codex':
            from .model_catalog import resolve_selection
            return resolve_selection(model, effort)
        catalog = self.models(provider)
        selected = next((m for m in catalog['models'] if m['id'] == (model or catalog['default_model'])), None)
        if not selected or effort not in selected['efforts']:
            raise ValueError('模型或推理强度不在该执行器的可选列表中，请刷新后重新选择。')
        return selected['id'], effort

    def runner(self, cfg, directory, session, metrics, container, study=None, token=None, **options):
        if self.engine == 'codex':
            from .codex_agent import CodexRunner
            return CodexRunner(cfg, directory, session, metrics, container, study, **options)
        from .agent import ClaudeRunner
        options.pop('game_server', None)
        return ClaudeRunner(cfg, directory, session, metrics, self.token(cfg) if token is None else token, container, study, **options)

    def archived(self, ident, root=None, *, state=None):
        if self.engine != 'codex':
            return False
        if state and state.get('session_storage') == 'container':
            return bool(state.get('archived'))
        from pathlib import Path
        return any(ident in path.name for path in (root or Path.home()/'.codex/archived_sessions').glob('*.jsonl'))


RUNTIMES = {r.engine:r for r in (Runtime('codex', 'Codex', fast=True, mcp=False, telemetry_source='codex_app_server',
                                       default_model='', default_effort='medium', default_knowledge='explore'),
                               Runtime('claude', 'Claude Code'))}


def runtime(engine):
    if not isinstance(engine, str) or engine not in RUNTIMES:
        raise ValueError('未知执行器：' + str(engine))
    return RUNTIMES[engine]


def configured_runtimes():
    return [{'id':adapter.engine+':'+provider['id'], 'engine':adapter.engine, 'provider':provider['id'],
             'label':adapter.label+' · '+provider['label'], 'fast':adapter.fast}
            for adapter in RUNTIMES.values() for provider in adapter.providers()]


def config_from_state(state, metrics=None):
    cfg = RunConfig(agent=state['agent'], save=state.get('save',''), model=state.get('requested_model') or state['model'],
                    effort=(metrics or {}).get('effort') or state['effort'],
                    knowledge_profile=state.get('knowledge_profile','explore'), runtime=state.get('runtime','container'))
    cfg.mcp_port = state.get('mcp_port', cfg.mcp_port)
    cfg.play_scope = state.get('play_scope', 'chapter')
    from .play_scope import completion_policy
    cfg.completion_policy = completion_policy(cfg.play_scope, state.get('completion_policy'))
    cfg.visited_maps = state.get('visited_maps')
    cfg.save_slot = state.get('save_slot')
    cfg.cutscene_mode = state.get('cutscene_mode', 'auto')
    # 挂给智能体的 /kb 与原运行相同（否则唤起会话时容器标签对不上，会被重建）
    cfg.kb_deny = list(state.get('kb_deny') or [])
    cfg.kb_extra = str(state.get('kb_extra') or '')
    from .action_policy import configure, permissions_from_state
    configure(cfg, permissions_from_state(state))
    cfg.allow_detour = bool(state.get('allow_detour', False)) and cfg.play_scope != 'visited'
    return runtime(state['engine']).configure(cfg, state.get('provider'))
