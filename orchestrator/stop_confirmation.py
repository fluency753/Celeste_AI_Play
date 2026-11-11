"""Machine-local wording for the stop_run confirmation receipt."""
from hashlib import sha256
import logging

from .session_store import load_json, locked_write, write_json
from .stop_categories import CONFIG_DIR


PATH = CONFIG_DIR / 'local.stop_confirmation.json'
MAX_CHARS = 4000
DEFAULT = ('核对受阻的任务必需步骤，以及是否还有获准的下一步能自己做。'
           '反复 observe 看到同一冻结帧不算恢复试验。'
           '确需用户帮助时，再次调用本工具。有可行下一步时直接继续，无需确认。')


def _text(value):
    if not isinstance(value, str) or len(value) > MAX_CHARS or '\x00' in value:
        raise ValueError(f'复核提示须为文本，最多 {MAX_CHARS} 字，不能含空字符')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('复核提示包含无效的 Unicode 字符') from exc
    return value.replace('\r\n', '\n').strip()


def _record(text):
    return {'text': text or DEFAULT, 'custom': bool(text), 'max_chars': MAX_CHARS,
            'version': sha256(text.encode()).hexdigest()}


def read():
    try:
        doc = load_json(PATH, encoding='utf-8-sig')
    except FileNotFoundError:
        doc = {'text': ''}
    if not isinstance(doc, dict) or set(doc) != {'text'}:
        raise ValueError(f'{PATH.name} 须包含一个 text 字段')
    return _record(_text(doc['text']))


def save(text, version):
    text = _text(text)
    text = '' if text == DEFAULT else text
    updated = _record(text)
    with locked_write(PATH.with_suffix('.json.lock')):
        current = read()
        if version != current['version']:
            raise ValueError('复核提示已被修改，请重新读取后保存')
        if updated['version'] != version:
            write_json(PATH, {'text': text}, indent=2)
    return updated


def wording():
    try:
        return read()['text']
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning('读取 %s 失败，本次复核使用默认提示：%s', PATH.name, exc)
        return DEFAULT
