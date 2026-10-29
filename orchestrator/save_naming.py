"""A save name is chosen by the active conversation, never by a host fallback."""
from .saves import validate_new_name

def name_prompt():
    from .button_prompts import notification
    return notification('name_save', facts=(
        '名字也用作存档显示名，游戏角色会在对话中用它称呼玩家。'
        '限1～20个可打印 ASCII 字符；中文需转为拼音以兼容游戏字体。'
        '最终回复只给名字本身，不加解释或格式。现在只取名，不创建存档、不操作游戏，也不改变会话显示名。'))


def parse_name(text):
    if not isinstance(text, str):
        raise ValueError('会话没有返回存档名')
    return validate_new_name(text.strip())
