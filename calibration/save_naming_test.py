import unittest
from orchestrator.save_naming import parse_name, name_prompt


class SaveNamingTests(unittest.TestCase):
    def test_name_uses_characters_supported_without_extra_fonts(self):
        for name in ['Xiaolan','North Star','Madeline','0','a'*20,'!@#$']:
            self.assertEqual(parse_name(name),name)
        for invalid in [None,'','  ','a'*21,'name\nreason','name\x00','小岚','é','🙂']:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):parse_name(invalid)

    def test_prompt_explains_font_limits_without_choosing_a_name(self):
        PROMPT = name_prompt()
        self.assertIn('ASCII',PROMPT)
        self.assertIn('拼音',PROMPT)
        self.assertNotIn('Xiaolan',PROMPT)
        self.assertNotIn('Madeline',PROMPT)
        self.assertIn('不创建存档',PROMPT)
        self.assertIn('玩家角色',PROMPT)
        self.assertIn('对话',PROMPT)

    def test_request_comes_first_and_name_usage_is_stated_once(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from orchestrator import prompting
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(prompting,'OVERRIDES',Path(folder)/'prompts'):
                text=name_prompt()
            self.assertEqual(text.count('存档显示名'),1)
            self.assertEqual(text.count('称呼'),1)
            request=text.index('请为玩家角色取一个')
            for limit in ('ASCII','拼音','最终回复只给名字本身','不创建存档'):
                self.assertLess(request,text.index(limit),limit)
            # 本机改写的取名请求同样排在主控的固定限制之前，限制仍然保留
            overrides=Path(folder)/'prompts'
            overrides.mkdir()
            (overrides/'button_name_save_zh.md').write_text('给角色起个名字。',encoding='utf-8')
            with patch.object(prompting,'OVERRIDES',overrides):
                custom=name_prompt()
            self.assertTrue(custom.startswith('【主控系统消息｜存档取名】\n给角色起个名字。\n'))
            self.assertIn('ASCII',custom)


if __name__=='__main__':unittest.main()
