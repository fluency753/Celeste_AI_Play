"""Execute real goal row editing and submit serialization for both duration fields."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class HudGoalDurationTests(unittest.TestCase):
    def test_duration_fields_default_to_relative_seconds_and_submit_numbers(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node is required')
        html=(Path(__file__).resolve().parent.parent/'overlay/hud.html').read_text(encoding='utf-8')
        source=html[html.index('const GOAL_FIELDS ='):html.index('function editGoal()')]
        source+=html[html.index('function goalRow('):html.index('function renderGoal(')]
        source+=html[html.index('async function saveGoal('):html.index("$('goalSave').onclick=")]
        harness=r'''
const fs=require('fs'),assert=require('assert');
class E {
 constructor(){this.children=[];this.value='';this.disabled=false;}
 add(o){this.children.push(o);if(!this.value)this.value=o.value;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;this.value=children[0]?.value||'';}
 setAttribute(){} remove(){}
}
class Option{constructor(text,value){this.text=text;this.value=value;}}
const elements={},$=id=>elements[id]||(elements[id]=new E()),document={createElement:()=>new E()};
let controlData={run:'run',control:{goal:{revision:1}}},goalError='',savingGoal=false,goalPending=null;
let sent=[];const editGoal=()=>{},renderGoal=()=>{},pollControl=async()=>{};
const sendControl=async(kind,text,onAccepted)=>{sent.push({kind,text});onAccepted({id:1});return true;};
eval(JSON.parse(fs.readFileSync(0,'utf8')).source+`
(async()=>{
 for(const field of ['game_elapsed','elapsed']){
  const row=goalRow();row.children[0].value=field;row.children[0].onchange();
  assert.equal(row.children[1].value,'delta_gte');assert.equal(row.children[2].value,'60');assert.equal(row.children[2].step,'any');
  row.children[2].value=field==='game_elapsed'?'2.5':'600';$('goalConditions').append(row);
 }
 $('goalText').value='自主建模并运行游戏实验';$('goalMatch').value='any';
 await saveGoal('goal');const data=JSON.parse(sent[0].text);
 assert.deepEqual(data.conditions,[{field:'game_elapsed',op:'delta_gte',value:2.5},{field:'elapsed',op:'delta_gte',value:600}]);
 assert.equal(data.match,'any');assert.equal(data.text,'自主建模并运行游戏实验');
 $('goalConditions').children[0].children[2].value='';await saveGoal('goal');assert.equal(sent.length,1);assert.match(goalError,/数值/);
 console.log('duration controls verified');
})().catch(e=>{console.error(e);process.exitCode=1;});`);
'''
        result=subprocess.run([node,'-e',harness],input=json.dumps({'source':source}),text=True,
                              capture_output=True,encoding='utf-8',check=True)
        self.assertIn('verified',result.stdout)


if __name__=='__main__':unittest.main()
