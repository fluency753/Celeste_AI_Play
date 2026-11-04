"""Verify the target container's model access before game or recording takeover."""
import asyncio
import sys

from .codex_container import CodexLoginRequired
from .codex_recovery import requires_login
from .codex_rpc import CodexRPC, CodexRPCError


async def verify_model(cfg, container, directory):
    completed = asyncio.Queue()
    thread_id = None

    def event(method, params):
        if method == 'turn/completed' and thread_id and params.get('threadId') == thread_id:
            completed.put_nowait(params['turn'])

    rpc = CodexRPC(directory, on_event=event, container=container)
    try:
        async with asyncio.timeout(75):
            await rpc.start()
            params = {'ephemeral': True, 'cwd': '/tmp', 'permissions': 'celeste-read',
                      'approvalPolicy': 'never', 'selectedCapabilityRoots': [],
                      'baseInstructions': 'This is a connection check. Reply only OK. Do not use tools.'}
            if cfg.model:
                params['model'] = cfg.model
            started = await rpc.request('thread/start', params)
            if (started.get('activePermissionProfile') or {}).get('id') != 'celeste-read':
                raise RuntimeError('Codex 未采用预检所需的只读权限')
            thread_id = started['thread']['id']
            turn = await rpc.request('turn/start', {'threadId': thread_id, 'effort': cfg.effort,
                'serviceTierForTurn': 'default', 'input': [{'type': 'text', 'text': 'Reply OK.'}]})
            turn_id = turn['turn']['id']
            while True:
                result = await completed.get()
                if result.get('id') == turn_id:
                    break
            if result.get('status') != 'completed':
                raise CodexRPCError(result.get('error') or result.get('status'), context='模型预检轮次未完成')
    except CodexLoginRequired:
        raise
    except RuntimeError as exc:
        if requires_login(exc):
            raise CodexLoginRequired(container) from exc
        raise
    except TimeoutError as exc:
        raise RuntimeError('等待 Codex 模型预检超时') from exc
    finally:
        primary = sys.exception()
        try:
            await rpc.close()
        except Exception as cleanup:
            if primary is None:
                raise
            primary.add_note(f'Codex 预检进程清理失败：{cleanup}')
