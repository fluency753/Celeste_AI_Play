"""Execute the actual HUD action-limit form against an isolated control response."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest

HUD = Path(__file__).resolve().parents[1]/'overlay/hud.html'


class _ChatForm(HTMLParser):
    def __init__(self):
        super().__init__();self.form=None
    def handle_starttag(self,tag,attrs):
        if tag=='form' and dict(attrs).get('id')=='agentControls':self.form=dict(attrs)


class HudChatSubmitTests(unittest.TestCase):
    """A draft in the frames box (or any panel field) must never silently block chat."""

    def test_chat_form_leaves_field_checks_to_their_apply_buttons(self):
        parser=_ChatForm();parser.feed(HUD.read_text(encoding='utf-8'))
        self.assertIsNotNone(parser.form)
        self.assertIn('novalidate',parser.form)

    def test_invalid_frames_draft_does_not_block_chat_and_is_still_checked_on_apply(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is needed to run the real HUD page')
        html=HUD.read_text(encoding='utf-8')
        snapshot={'run':'run1','sessions':[],'session_management':False,'control':{
            'connected':True,'paused':True,'act_limit_control':True,'actions':[],'messages':[],
            'act_limit_settings':{'frames':150,'min_frames':40,'min':30,'max':3600}}}
        posts=[]

        def handle(route):
            request=route.request;url=request.url
            if not url.startswith('http://hud.test/'):
                return route.abort()      # never reach a real controller or HUD service
            if url=='http://hud.test/hud':
                return route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
            if url.startswith('http://hud.test/api/control'):
                if request.method=='POST':
                    posts.append(json.loads(request.post_data))
                    return route.fulfill(status=200,content_type='application/json',body=json.dumps({'id':len(posts)}))
                return route.fulfill(status=200,content_type='application/json',body=json.dumps(snapshot))
            if url.startswith('http://hud.test/hud/state'):
                return route.fulfill(status=200,content_type='application/json',body=json.dumps(
                    {'now':time.time(),'config':{},'metrics':{'status':'paused','agent':'hud','model':'hud'}}))
            return route.fulfill(status=404,content_type='application/json',body='{}')

        def sent(kind):
            return [item for item in posts if item['kind']==kind]

        with sync_playwright() as playwright:
            try:
                browser=playwright.chromium.launch()
            except Exception as error:  # noqa: BLE001 - browser binaries are optional locally
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                page=browser.new_page();page.route('**/*',handle)
                page.goto('http://hud.test/hud')
                page.wait_for_function("!document.getElementById('agentSend').disabled")
                draft="v=>{const f=document.getElementById('actLimitFrames');f.value=v;f.dispatchEvent(new Event('input'));}"
                for value in ('','5000'):   # panel collapsed: no hint would be visible
                    page.evaluate(draft,value)
                    page.fill('#agentInput',f'提示 {value!r}')
                    page.press('#agentInput','Enter')
                    page.wait_for_function("document.getElementById('agentInput').value===''",timeout=3000)
                    self.assertEqual(sent('message')[-1]['text'],f'提示 {value!r}')
                self.assertEqual(len(sent('message')),2)
                self.assertEqual(sent('act_limit'),[])
                self.assertEqual(page.input_value('#actLimitFrames'),'5000')   # the draft is kept
                # The field's own check still runs on apply (button or Enter in the box).
                page.click('#actLimitPanel > summary')
                page.click('#actLimitSave');page.press('#actLimitFrames','Enter');page.wait_for_timeout(300)
                self.assertFalse(page.evaluate("document.getElementById('actLimitFrames').validity.valid"))
                self.assertEqual(sent('act_limit'),[])
                self.assertEqual(len(sent('message')),2)
                page.fill('#actLimitFrames','120');page.click('#actLimitSave')
                page.wait_for_function("document.getElementById('actLimitStatus').textContent.includes('等待')",timeout=3000)
                self.assertEqual([json.loads(item['text']) for item in sent('act_limit')],[{'frames':120}])
            finally:
                browser.close()


class HudActLimitTests(unittest.TestCase):
    def test_form_preserves_draft_waits_for_ack_and_handles_errors_old_hosts_and_session_changes(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        html = (Path(__file__).resolve().parents[1]/'overlay/hud.html').read_text(encoding='utf-8')
        source = html[html.index('let actLimitRun='):html.index('function renderSimulation(')]
        harness = r'''
const fs=require('fs'),assert=require('assert');
class Element {constructor(){this.value='';this.disabled=false;this.textContent='';}reportValidity(){return this.value!==''&&Number.isInteger(Number(this.value))&&Number(this.value)>=Number(this.min)&&Number(this.value)<=Number(this.max);}}
const ids={};for(const id of ['actLimitFrames','actLimitSave','actLimitReset','actLimitPanel','actLimitStatus'])ids[id]=new Element();
ids.actLimitPanel.querySelectorAll=()=>[ids.actLimitFrames,ids.actLimitSave,ids.actLimitReset];const $=id=>ids[id];
let switchingSession=false,controlData={run:'run1',control:{connected:true,act_limit_control:true,actions:[],act_limit_settings:{frames:150,min_frames:40,min:30,max:3600}}};
const CONTROL_URL='/api/control';let sent=[],ident=5;
const fetch=async(url,options)=>{sent.push(JSON.parse(options.body));return {ok:true,json:async()=>({id:ident++})};};
const pollControl=async()=>renderActLimit(controlData.control);
eval(JSON.parse(fs.readFileSync(0,'utf8')).source+`
(async()=>{
 const c=controlData.control;renderActLimit(c);assert.equal($('actLimitFrames').value,'150');
 $('actLimitFrames').value='300';actLimitEdited=true;c.act_limit_settings.frames=200;renderActLimit(c);assert.equal($('actLimitFrames').value,'300');
 await saveActLimit();assert.equal(sent[0].kind,'act_limit');assert.deepEqual(JSON.parse(sent[0].text),{frames:300});assert.equal($('actLimitSave').disabled,true);assert.match($('actLimitStatus').textContent,/等待/);
 c.actions=[{id:5,status:'submitted'}];c.act_limit_settings.frames=300;renderActLimit(c);assert.equal($('actLimitSave').disabled,false);assert.match($('actLimitStatus').textContent,/已生效/);
 $('actLimitFrames').value='301';actLimitEdited=true;await saveActLimit();c.actions=[{id:6,status:'error',detail:'写入失败'}];renderActLimit(c);assert.match($('actLimitStatus').textContent,/写入失败/);assert.equal($('actLimitFrames').value,'301');
 controlData={run:'run2',control:{...c,actions:[],act_limit_settings:{...c.act_limit_settings,frames:600}}};renderActLimit(controlData.control);assert.equal($('actLimitFrames').value,'600');
 $('actLimitFrames').value='3601';actLimitEdited=true;await saveActLimit();assert.equal(sent.length,2);
 controlData={run:'old',control:{connected:true}};renderActLimit(controlData.control);assert.equal($('actLimitSave').disabled,true);assert.match($('actLimitStatus').textContent,/更新主控/);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'source': source}), capture_output=True,
                                text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
