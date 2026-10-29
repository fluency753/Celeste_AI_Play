"""Execute the actual HUD action-limit form against an isolated control response."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


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
