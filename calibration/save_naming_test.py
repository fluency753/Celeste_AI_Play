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


if __name__=='__main__':unittest.main()
