"""A save name is chosen by the active conversation, never by a host fallback."""
from .saves import validate_new_name

def name_prompt():
    from .button_prompts import notification
    # 名字的用途（存档显示名、角色称呼）只在正文模板里说一次；这里是主控核验的固定限制，排在取名请求之后
    return notification('name_save', facts=(
        '取名限制：1～20个可打印 ASCII 字符；中文需转为拼音以兼容游戏字体。'
        '最终回复只给名字本身，不加解释或格式。现在只取名，不创建存档、不操作游戏，也不改变会话显示名。'))


def parse_name(text):
    if not isinstance(text, str):
        raise ValueError('会话没有返回存档名')
    return validate_new_name(text.strip())
