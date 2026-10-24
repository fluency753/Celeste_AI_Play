"""A save name is chosen by the active conversation, never by a host fallback."""
PROMPT = ('请为玩家角色取一个你喜欢的名字。它也用作存档显示名，游戏中的角色会在对话里用这个名字称呼你。'
          '最多20个字符，名字由你决定。'
          '最终回复只给出名字本身，不加解释或额外格式。'
          '现在只取名，不创建存档、不操作游戏，也不改变会话显示名。')


def parse_name(text):
    if not isinstance(text, str):
        raise ValueError('会话没有返回存档名')
    name = text.strip()
    if not name or len(name) > 20 or not name.isprintable() or any(ord(c) > 0xffff for c in name):
        raise ValueError('会话返回的存档名不符合游戏的1～20字符限制，请重新开始或自行填写')
    return name
