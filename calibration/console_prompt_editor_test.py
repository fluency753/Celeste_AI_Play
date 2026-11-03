"""Editing a button message must reveal and focus the actual editor, including stacked layouts."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class ConsolePromptEditorTests(unittest.TestCase):
    def test_removing_single_section_override_shows_inherited_text_and_source(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        source = (Path(__file__).resolve().parents[1]/'overlay/console.html').read_text(encoding='utf-8')
        functions = source[source.index('async function savePromptFile('):source.index('async function loadAbout(')]
        harness = r'''
const fs=require('fs'),assert=require('assert');
const ids={},messages=[];
const $=id=>ids[id] ||= {value:'',textContent:'',disabled:false,classList:{toggle(){}}};
const ui={promptFile:{name:'tool_act_zh.md',version:'old',dirty:true}};
const postJSON=async()=>({name:'tool_act_zh.md',text:'继承的动作说明',version:'new',overridden:false,
 inherited_from:'interface_zh.md',source_path:'orchestrator/prompts/shared/interface_zh.md'});
const toast=message=>messages.push(message),updatePromptButtons=()=>{},loadPromptPreview=()=>{};
eval(JSON.parse(fs.readFileSync(0,'utf8')).functions+`
(async()=>{
 $('promptText').value='仓库默认说明';
 await savePromptFile();
 assert.equal($('promptText').value,'继承的动作说明');
 assert($('promptStatus').textContent.includes('继承 interface_zh.md'));
 assert.equal(ui.promptFile.dirty,false);assert.equal($('promptReset').disabled,true);
 $('promptText').value='单节修改';
 await resetPromptFile('tool_act_zh.md');
 assert.equal($('promptText').value,'继承的动作说明');
 assert($('promptStatus').textContent.includes('继承 interface_zh.md'));
 assert(messages[0].includes('继承 interface_zh.md'));
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'functions': functions}),
                                text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)

    def test_opening_message_reveals_editor_scrolls_to_it_and_preserves_dirty_changes(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        source = (Path(__file__).resolve().parents[1]/'overlay/console.html').read_text(encoding='utf-8')
        function = source[source.index('async function openPromptFile('):source.index('function confirmLeave()')]
        harness = r'''
const fs=require('fs'),assert=require('assert');
const ids={};let scrolling=0,focused=0,requests=0,confirmed=false;
const $=id=>ids[id] ||= {hidden:true,textContent:'',value:'',classList:{remove(){}},replaceChildren(){},scrollIntoView(options){assert.equal(options.block,'start');scrolling++;},focus(options){assert.equal(options.preventScroll,true);focused++;}};
const row={dataset:{promptName:'button_resume_zh.md'}};
const document={querySelectorAll:()=>[row]};
const ui={promptFile:null};const confirmLeave=()=>confirmed;const updatePromptButtons=()=>{};const profileReadonly=()=>false;
const getJSON=async url=>{requests++;assert(url.includes('button_resume_zh.md'));return {name:'button_resume_zh.md',text:'当前消息正文',placeholders:[],overridden:false,live_editable:true};};
const toast=message=>{throw Error(message);};
eval(JSON.parse(fs.readFileSync(0,'utf8')).function+`
(async()=>{
 await openPromptFile('button_resume_zh.md');
 assert.equal($('promptEditor').hidden,false);assert.equal($('promptPreviewBox').hidden,true);
 assert.equal($('promptText').value,'当前消息正文');assert.equal(scrolling,1);assert.equal(focused,1);assert.equal(row.dataset.selected,'true');
 ui.promptFile.dirty=true;ui.promptFile.name='other.md';$('promptText').value='未保存修改';
 await openPromptFile('button_resume_zh.md');assert.equal(requests,1);assert.equal($('promptText').value,'未保存修改');assert.equal(scrolling,1);
 ui.promptFile.name='button_resume_zh.md';
 await openPromptFile('button_resume_zh.md');assert.equal(requests,1);assert.equal($('promptText').value,'未保存修改');assert.equal(scrolling,2);assert.equal(focused,2);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'function': function}),
                                text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
