"""Local personal requirements: real HTTP persistence and browser draft lifecycle."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from orchestrator import personal_prompts
from overlay.server import ThreadingHTTPServer, make_handler


class PersonalPromptHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'local' / 'personal_prompts.json'
        self.store_patch = patch.object(personal_prompts, 'STORE', self.path)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}/api/console/personal-prompt'

    def close_server(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()

    def request(self, scope='all', data=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        request = Request(self.base + ('?scope=' + scope if data is None else ''),
                          data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
                          headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_empty_read_save_distinct_scopes_conflict_and_clear(self):
        code, common = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(common['text'], '')
        self.assertEqual(len(common['catalog']), 5)
        self.assertFalse(self.path.parent.exists())
        _, specific = self.request('codex:explore')
        code, saved = self.request(data={'scope': 'all', 'text': '保持反馈与验证', 'version': common['version']})
        self.assertEqual((code, saved['text']), (200, '保持反馈与验证'))
        code, result = self.request(data={'scope': 'codex:explore', 'text': '自行建模', 'version': specific['version']})
        self.assertEqual(code, 200)
        self.assertEqual(self.request()[1]['text'], saved['text'])
        code, error = self.request(data={'scope': 'all', 'text': '旧版本', 'version': common['version']})
        self.assertEqual(code, 400)
        self.assertIn('重新读取', error['error'])
        self.assertEqual(self.request()[1]['text'], saved['text'])
        self.assertEqual(self.request(data={'scope': 'all', 'text': '', 'version': saved['version']})[0], 200)
        self.assertEqual(self.request()[1]['text'], '')
        self.assertEqual(self.request('codex:explore')[1]['text'], result['text'])

    def test_invalid_requests_and_corruption_do_not_overwrite(self):
        self.assertEqual(self.request('invalid')[0], 400)
        empty = self.request()[1]
        data = {'scope': 'all', 'text': 'test', 'version': empty['version']}
        self.assertEqual(self.request(data=data, origin='https://example.com')[0], 400)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir()
        self.path.write_bytes(b'broken')
        self.assertEqual(self.request()[0], 400)
        self.assertEqual(self.request(data=data)[0], 400)
        self.assertEqual(self.path.read_bytes(), b'broken')

    def test_maximum_unicode_text_using_escaped_json_is_accepted(self):
        version = self.request()[1]['version']
        text = '中' * personal_prompts.MAX_TEXT_CHARS
        request = Request(self.base, data=json.dumps({'scope': 'all', 'text': text, 'version': version}).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(json.load(response)['text'], text)
        self.assertEqual(self.request()[1]['text'], text)


class PersonalPromptBrowserTests(unittest.TestCase):
    def test_late_preview_success_and_error_cannot_replace_latest_preview(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        html = (Path(__file__).resolve().parents[1] / 'overlay/console.html').read_text(encoding='utf-8')
        function = html[html.index('let promptPreviewRequest ='):html.index('async function openPromptFile(')]
        harness = r'''
const assert=require('assert'),fs=require('fs'),pending=[];
const ui={}, elements={};
const $=id=>elements[id] ||= {textContent:'',classList:{add(){},remove(){}},replaceChildren(...items){this.children=items;}};
const document={createElement:()=>({})};
const promptOptions=()=>({mode:'explore'});
const postJSON=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
const doc=text=>({text,parts:[],executor:text,overridden:[],personal_applied:[{label:text}]});
eval(JSON.parse(fs.readFileSync(0,'utf8')).function+`
(async()=>{
 let old=loadPromptPreview(), fresh=loadPromptPreview();
 pending[1].resolve(doc('新个人要求'));await fresh;
 assert.equal(ui.prompt.text,'新个人要求');
 pending[0].resolve(doc('旧个人要求'));await old;
 assert.equal(ui.prompt.text,'新个人要求');assert.equal($('promptExecutor').textContent,'新个人要求');
 assert($('promptWhat').textContent.includes('新个人要求'));assert(!$('promptWhat').textContent.includes('旧个人要求'));
 let oldError=loadPromptPreview(), newest=loadPromptPreview();
 pending[3].resolve(doc('最新版本'));await newest;
 pending[2].reject(Error('过期错误'));await oldError;
 assert.equal(ui.prompt.text,'最新版本');assert.equal($('promptExecutor').textContent,'最新版本');
 assert($('promptWhat').textContent.includes('最新版本'));
 // A current failure should still clear copied/visible preview state.
 let failure=loadPromptPreview();pending[4].reject(Error('当前错误'));await failure;
 assert.equal(ui.prompt,null);assert($('promptWhat').textContent.includes('当前错误'));
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'function': function}),
                                text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)

    def test_scope_drafts_late_reads_and_save_edits(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        html = (Path(__file__).resolve().parents[1] / 'overlay/console.html').read_text(encoding='utf-8')
        functions = html[html.index('const personalDrafts ='):html.index('function promptText(text)')]
        harness = r'''
const assert=require('assert'), fs=require('fs');
const elements={};const $=id=>elements[id] ||= {value:'',disabled:false,textContent:'',replaceChildren(...options){this.options=options;}};
const document={createElement:()=>({})}; const confirm=()=>true; const loadPromptPreview=()=>{};
const reads=[],writes=[];
const getJSON=url=>new Promise((resolve,reject)=>reads.push({url,resolve,reject}));
const postJSON=(url,body)=>new Promise((resolve,reject)=>writes.push({url,body,resolve,reject}));
const catalog=[{scope:'all',label:'通用'},{scope:'codex:explore',label:'探索'}];
const doc=(scope,text,version)=>({scope,text,version,catalog,max_chars:12000});
eval(JSON.parse(fs.readFileSync(0,'utf8')).functions+`
(async()=>{
 $('personalPromptScope').value='all';
 let first=loadPersonalPrompt();
 $('personalPromptScope').value='codex:explore';++personalRequest;
 let second=loadPersonalPrompt();
 reads[1].resolve(doc('codex:explore','探索','v1'));await second;
 reads[0].resolve(doc('all','旧结果','v0'));await first;
 assert.equal($('personalPromptText').value,'探索');
 let specific=personalDrafts.get('codex:explore');specific.text='未保存草稿';
 $('personalPromptScope').value='all';++personalRequest;
 let commonLoad=loadPersonalPrompt();reads[2].resolve(doc('all','通用','a1'));await commonLoad;
 $('personalPromptScope').value='codex:explore';++personalRequest;await loadPersonalPrompt();
 assert.equal($('personalPromptText').value,'未保存草稿');
 let saving=savePersonalPrompt();assert.equal(writes[0].body.text,'未保存草稿');
 specific.text='发送后继续编辑';
 writes[0].resolve(doc('codex:explore','未保存草稿','v2'));await saving;
 assert.equal(specific.version,'v2');assert.equal(specific.text,'发送后继续编辑');assert.equal(specific.saved,'未保存草稿');
 assert.equal($('personalPromptSave').disabled,false);
 let reload=loadPersonalPrompt(true);specific.text='读取期间新输入';reads[3].resolve(doc('codex:explore','磁盘内容','v3'));await reload;
 assert.equal(specific.text,'读取期间新输入');assert.equal(specific.version,'v2');
 let clearing=savePersonalPrompt(true);assert.equal(writes[1].body.version,'v2');assert.equal(writes[1].body.text,'');
 $('personalPromptScope').value='all';++personalRequest;await loadPersonalPrompt();
 writes[1].resolve(doc('codex:explore','','v4'));await clearing;
 assert.equal($('personalPromptText').value,'通用');assert.equal(specific.saved,'');
 assert.equal(personalDrafts.get('all').version,'a1');
 let common=personalDrafts.get('all');common.text='即将保存';
 let pendingRead=loadPersonalPrompt(true), pendingSave=savePersonalPrompt();
 reads[4].resolve(doc('all','过时读取','old'));await pendingRead;
 writes[2].resolve(doc('all','即将保存','a2'));await pendingSave;
 assert.strictEqual(personalDrafts.get('all'),common);assert.equal(common.version,'a2');assert.equal(common.text,'即将保存');
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'functions': functions}),
                                text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
