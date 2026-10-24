"""Execute the actual HUD button state function, including old-chat/new-game handoff."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class PrimaryControlTests(unittest.TestCase):
    def test_labels_and_click_actions_follow_controller_lifecycle(self):
        node=shutil.which('node')
        if not node:
            self.skipTest('Node is needed to execute HUD JavaScript')
        html=(Path(__file__).resolve().parent.parent/'overlay/hud.html').read_text(encoding='utf-8')
        source=html[html.index('function primaryControlView('):html.index('\nconst CONTROL_URL')]
        cases=[]
        def case(data,label,action=None,local=None):
            cases.append({'data':data,'local':local or {},'expected':{'label':label,'disabled':action is None,'action':action}})
        old={'control':{'connected':False},'can_wake':True}
        case({**old,'operation':{'status':'naming'}},'取名中…')
        for stage,label in [('preparing','准备中…'),('connecting','接入中…'),('starting','启动中…')]:
            case({**old,'operation':{'status':stage}},label)
        case({'control':{'connected':True,'paused':False},'can_wake':True},'暂停','pause')
        case({'control':{'connected':True,'paused':True}},'继续','resume')
        case({'control':{'connected':True,'phase':'pausing'}},'暂停中…')
        case({'control':{'connected':True,'actions':[{'kind':'resume','status':'queued'}]}},'恢复中…')
        case(old,'唤起 agent','wake')
        case({**old,'ready_to_play':True},'连接 agent','wake')
        case({'control':{'connected':False},'can_wake':False},'未连接')
        case({'control':{'connected':True,'chat_only':True},'ready_to_play':True},'待开始')
        case({'control':{'connected':True,'chat_only':True}},'试玩已结束')
        case({**old,'waking':True},'唤起中…')
        case(old,'切换中…',local={'switchingSession':True})
        case(old,'处理中…',local={'lifecycleBusy':True})
        case({'control':{'connected':True,'actions':[{'kind':'fast','status':'queued'}]}},'暂停','pause')
        case(None,'未连接')
        script="""const fs=require('fs'),vm=require('vm');const p=JSON.parse(fs.readFileSync(0,'utf8'));
const f=vm.runInNewContext(p.source+';primaryControlView');
process.stdout.write(JSON.stringify(p.cases.map(c=>f(c.data,c.local))));"""
        result=subprocess.run([node,'-e',script],input=json.dumps({'source':source,'cases':cases}),
                              capture_output=True,text=True,encoding='utf-8',check=True)
        actual=json.loads(result.stdout)
        for index,case in enumerate(cases):
            with self.subTest(index=index,label=case['expected']['label']):
                self.assertEqual(actual[index],case['expected'])


if __name__=='__main__':unittest.main()
