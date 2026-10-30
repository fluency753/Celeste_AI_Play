"""Choose a verified observation file or inline geometry, never a stale pointer."""
import json

from .collision_snapshot import current_collision, collision_summary
from .geometry_context import GeometryContext, observation_id
from .observation import room_json


FILE_JSON_HEADER = '当前观测文件：\n'
ROOM_JSON_HEADER = '房间布局（精确实例位置；共享属性与形状，后续按快照返回变化）：\n'


def observation_file(session, obs):
    if not session.workspace:
        return None
    try:
        path = session.workspace / 'game' / 'latest_obs.json'
        # A path, successful collision-image write, or equal frame alone is not proof
        # that the complete current observation reached the consumer's file.
        limit = 16 * 1024 * 1024
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None
        published = json.loads(raw.decode('utf-8'))
        if not isinstance(published, dict) or published.get('obs') != obs:
            return None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    level = obs.get('level') or {}
    collision = current_collision(obs)
    return {
        'encoding': 'workspace-files-v1', 'frame': obs.get('frame'),
        'sid': level.get('sid'), 'room': level.get('room'), 'snapshot_id': observation_id(obs),
        'snapshot_id_scope': 'geometry-cache',
        'files': {'observation': '/workspace/game/latest_obs.json', 'last_tool': '/workspace/game/last_tool.json'},
        'collision': ({**{key: collision[key] for key in
                          ('scope', 'bounds', 'coverage', 'complete', 'geometryComplete')},
                       'object_count': len(collision['objects'])} if collision is not None else None),
        'collisionNote': collision_summary(obs) if collision is None else
                         '完整来源和精确形状在观测文件的obs.collision；按需读取，不要整份打印。',
    }


def compact_reference(reference: dict) -> str:
    """节省上下文：同一房间里观测文件只给一行（路径、帧号、快照编号）；碰撞数据覆盖不完整时才写明缺什么。"""
    text = (f"观测文件：{reference['files']['observation']}（帧 {reference.get('frame')}，"
            f"快照 {reference.get('snapshot_id')}）")
    collision = reference.get('collision')
    if collision is None or not (collision.get('complete') and collision.get('geometryComplete')):
        text += '；碰撞数据：' + json.dumps({'collision': collision, 'collisionNote': reference.get('collisionNote')},
                                       ensure_ascii=False, separators=(',', ':'))
    return text


class ObservationDelivery:
    def __init__(self):
        self.geometry = GeometryContext()
        self.mode = None

    def prepare(self, session, obs, *, files_enabled, force=False, compact=False):
        reference = observation_file(session, obs) if files_enabled else None
        if reference is not None:
            if compact:
                return compact_reference(reference), True, ('file', None)
            return FILE_JSON_HEADER + json.dumps(reference, ensure_ascii=False, separators=(',', ':')), True, ('file', None)
        geometry, candidate = self.geometry.prepare(obs, force=force or self.mode == 'file')
        if force or geometry['mode'] == 'baseline':
            text = ROOM_JSON_HEADER + room_json(obs, compact_context=True, geometry_context=geometry)
        else:
            text = '当前帧的统一碰撞数据（变化）：\n' + json.dumps(geometry, ensure_ascii=False, separators=(',', ':'))
        return text, False, ('inline', candidate)

    def commit(self, candidate):
        mode, geometry = candidate
        if mode == 'inline':
            self.geometry.commit(geometry)
        self.mode = mode
