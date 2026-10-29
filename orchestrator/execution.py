"""Actual execution locations, independent of the legacy ``runtime`` label.

Both agents run their programming tools in Docker. Codex app-server itself
also runs there. Claude's legacy host client has only game MCP tools.
Consumers use this contract instead of inferring permissions from placement.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceCapabilities:
    agent_location: str
    workspace_read: bool
    workspace_write: bool
    program_location: str | None
    builtin_tools: bool = False

    @property
    def programs(self) -> bool:
        return self.program_location is not None

    @property
    def needs_container(self) -> bool:
        return self.agent_location == 'container' or self.program_location == 'container'

    def validate(self, *, programs=False, sim=False, preparation=False):
        if sim and not programs:
            raise ValueError('离线模拟器需要同时开启 --allow-programs')
        if programs and not self.programs:
            raise ValueError('工作区程序和离线模拟器需要 container 工具通道；host Claude 会话仅支持聊天')
        if preparation and not self.workspace_read:
            raise ValueError('当前执行器没有知识库读取工具，不能进行知识库准备')


_CAPABILITIES = {
    ('codex', 'host'): WorkspaceCapabilities('container', True, True, 'container', True),
    ('codex', 'container'): WorkspaceCapabilities('container', True, True, 'container', True),
    ('claude', 'host'): WorkspaceCapabilities('host', False, False, None),
    ('claude', 'container'): WorkspaceCapabilities('container', True, True, 'container', True),
}


def workspace_capabilities(cfg) -> WorkspaceCapabilities:
    key = (getattr(cfg, 'engine', 'claude'), getattr(cfg, 'runtime', 'container'))
    try:
        return _CAPABILITIES[key]
    except KeyError:
        raise ValueError(f'未知执行环境：{key[0]}/{key[1]}') from None
