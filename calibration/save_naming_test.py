import unittest
from orchestrator.save_naming import parse_name, PROMPT


class SaveNamingTests(unittest.TestCase):
    def test_free_name_with_only_game_input_limits(self):
        for name in ['雪','North Star','Madeline','0','a'*20,'!@#$','星 & 雪']:
            self.assertEqual(parse_name(name),name)
        for invalid in [None,'','  ','a'*21,'name\nreason','name\x00']:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):parse_name(invalid)

    def test_prompt_does_not_suggest_name_or_language(self):
        self.assertNotIn('中文',PROMPT)
        self.assertNotIn('英文',PROMPT)
        self.assertNotIn('Madeline',PROMPT)
        self.assertIn('不创建存档',PROMPT)
        self.assertIn('玩家角色',PROMPT)
        self.assertIn('对话',PROMPT)


if __name__=='__main__':unittest.main()
