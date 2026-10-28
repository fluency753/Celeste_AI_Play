"""Run the actual HUD draft and start handler, without a game or model."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orchestrator.launch_options import recording_options


class HudRecordingTests(unittest.TestCase):
    def test_old_run_and_new_prepared_defaults(self):
        self.assertEqual(recording_options({})['recording_mode'],'chapter')
        old=recording_options({},legacy=True)
        self.assertEqual(old,{'recording_mode':'continuous','freeze_chapter_select':False,
                              'freeze_file_select':False,'freeze_main_menu':False})
        self.assertEqual(recording_options({'recording_mode':'chapter','freeze_main_menu':True},legacy=True)['recording_mode'],'chapter')

    def test_poll_preserves_draft_switches_sessions_and_start_sends_all_settings(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node is required')
        html=(Path(__file__).resolve().parent.parent/'overlay/hud.html').read_text(encoding='utf-8')
        functions=html[html.index('let recordingSelectionRun='):html.index("let lifecycleBusy=")]
        button=html[html.index("$('sessionPlay').onclick="):html.index('async function sendControl(')]
        script=r'''
const assert=require('assert'),fs=require('fs'),source=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={};const $=id=>elements[id] || (elements[id]={value:'',checked:false,disabled:false,textContent:''});
const defaults={recording_mode:'chapter',freeze_chapter_select:true,freeze_file_select:false,freeze_main_menu:false};
const data={run:'prepared1',session_management:true,recording_options:defaults,control:{connected:true,chat_only:true}};
let sent=null,lifecycleError='';const pollControl=()=>{},selectedSave=()=>({save:'test',save_slot:10,create_save:false});
const sessionAction=(kind,options)=>{sent={kind,options};};
eval(source.functions+source.button+`
syncRecordingOptions(data);
assert.equal($('recordingMode').value,'chapter');assert.equal($('freezeChapterSelect').checked,true);
$('recordingMode').value='continuous';$('freezeChapterSelect').checked=false;$('freezeFileSelect').checked=true;$('freezeMainMenu').checked=true;
syncRecordingOptions(data);assert.equal($('recordingMode').value,'continuous');assert.equal($('freezeFileSelect').checked,true);
$('sessionChapter').value='0';$('sessionScope').value='campaign';$('sessionPlay').onclick();
assert.equal(sent.kind,'play_session');assert.equal(sent.options.recording_mode,'continuous');
assert.equal(sent.options.freeze_chapter_select,false);assert.equal(sent.options.freeze_file_select,true);assert.equal(sent.options.freeze_main_menu,true);
syncRecordingOptions({...data,run:'prepared2'});assert.equal($('recordingMode').value,'chapter');assert.equal($('freezeFileSelect').checked,false);
syncRecordingOptions({...data,run:'live',control:{connected:true,chat_only:false},recording_options:{...defaults,recording_mode:'continuous'}});
assert.equal($('recordingMode').value,'continuous');assert.equal($('recordingMode').disabled,true);
syncRecordingOptions({...data,run:'pending'},true);assert.equal($('freezeMainMenu').disabled,true);
syncRecordingOptions({...data,run:'pinned',session_management:false});assert.equal($('recordingMode').disabled,true);
console.log('HUD draft and play payload verified');
`);
'''
        result=subprocess.run([node,'-e',script],input=json.dumps({'functions':functions,'button':button}),
                              text=True,capture_output=True,encoding='utf-8',check=True)
        self.assertIn('verified',result.stdout)


if __name__=='__main__':unittest.main()
