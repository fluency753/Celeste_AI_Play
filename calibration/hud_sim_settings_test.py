"""执行实际 HUD 推演表单脚本，验证编辑、异步确认和错误反馈。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class HudSimulationTests(unittest.TestCase):
    def test_actual_form_does_not_overwrite_edits_or_send_untouched_balance(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required')
        html = (Path(__file__).resolve().parents[1]/'overlay/hud.html').read_text(encoding='utf-8')
        source = html[html.index('let simRun='):html.index('function goalRow(')]
        harness = r'''
const fs=require('fs'),assert=require('assert');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.disabled=false;}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=[...items];}
 setAttribute(){}
 querySelectorAll(){return this.children.flatMap(c=>[c,...c.querySelectorAll()]).filter(c=>['input','button'].includes(c.tagName));}
 querySelector(selector){const key=selector.match(/data-key="([^"]+)"/)[1];return this.querySelectorAll().find(c=>c.dataset.key===key);}
 reportValidity(){return !(this.required && this.value==='') && (this.value==='' || Number.isInteger(Number(this.value)) && Number(this.value)>=Number(this.min) && Number(this.value)<=Number(this.max));}
}
const ids={};for(const id of ['simFields','simBalance','simPanel','simApply','simReset','simStatus','simCurrent'])ids[id]=new Element();
ids.simApply.tagName=ids.simReset.tagName='button';ids.simBalance.tagName='input';ids.simBalance.min=0;ids.simBalance.max=100;
ids.simPanel.append(ids.simBalance,ids.simFields,ids.simApply,ids.simReset);
const $=id=>ids[id],document={createElement:tag=>new Element(tag)};
let switchingSession=false,controlData={run:'run1',control:{connected:true,sim_control:true,actions:[],sim_settings:{energy:{balance:60,capacity:100},rules:{assist_cost_base:2,assist_capacity:100},parameters:[{key:'assist_cost_base',label:'费用',min:0,max:1000,unit:'点'},{key:'assist_capacity',label:'上限',min:1,max:10000,unit:'点'}]}}};
const CONTROL_URL='/api/control';let sent=[],ident=5;
const fetch=async(url,options)=>{sent.push(JSON.parse(options.body));return {ok:true,json:async()=>({id:ident++})};};
const pollControl=async()=>renderSimulation(controlData.control);
eval(JSON.parse(fs.readFileSync(0,'utf8')).source+`
(async()=>{
 const c=controlData.control;
 renderSimulation(c);
 const fee=$('simFields').querySelector('[data-key="assist_cost_base"]');
 const capacity=$('simFields').querySelector('[data-key="assist_capacity"]');
 assert.equal(fee.value,'2');fee.value='7';fee.oninput();
 c.sim_settings.rules.assist_cost_base=3;c.sim_settings.rules.assist_capacity=120;
 renderSimulation(c);assert.equal(fee.value,'7');assert.equal(capacity.value,'120');
 await saveSimulation();assert.deepEqual(JSON.parse(sent[0].text),{rules:{assist_cost_base:7}});
 assert.equal($('simApply').disabled,true);assert.match($('simStatus').textContent,/等待/);
 c.actions=[{id:5,status:'submitted'}];c.sim_settings.rules.assist_cost_base=7;renderSimulation(c);
 assert.equal($('simApply').disabled,false);assert.match($('simStatus').textContent,/已生效/);
 fee.value='99';fee.oninput();await saveSimulation();c.actions=[{id:6,status:'error',detail:'非法费用'}];renderSimulation(c);
 assert.match($('simStatus').textContent,/非法费用/);assert.equal(fee.value,'99');
 controlData={run:'run2',control:{...c,actions:[]}};renderSimulation(controlData.control);
 assert.equal($('simFields').querySelector('[data-key="assist_cost_base"]').value,'7');
 const cap=$('simFields').querySelector('[data-key="assist_capacity"]');cap.value='200';cap.oninput();
 $('simBalance').value='150';await saveSimulation();assert.deepEqual(JSON.parse(sent[2].text),{rules:{assist_capacity:200},balance:150});
 controlData={run:'old',control:{connected:true,sim_control:false}};renderSimulation(controlData.control);
 assert.equal($('simApply').disabled,true);assert.match($('simStatus').textContent,/新版主控/);
 console.log('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
`);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'source':source}),
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
