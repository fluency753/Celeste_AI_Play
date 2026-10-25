"""A save name is chosen by the active conversation, never by a host fallback."""
from .saves import validate_new_name

PROMPT = ('请为玩家角色取一个你喜欢的名字。它也用作存档显示名，游戏中的角色会在对话里用这个名字称呼你。'
          '名字由你决定，限1～20个可打印 ASCII 字符；如想用中文名字，请转为拼音，避免游戏字体缺字。'
          '最终回复只给出名字本身，不加解释或额外格式。'
          '现在只取名，不创建存档、不操作游戏，也不改变会话显示名。')


def parse_name(text):
    if not isinstance(text, str):
        raise ValueError('会话没有返回存档名')
    return validate_new_name(text.strip())
