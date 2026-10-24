"""The actual frontend request identifies existing saves by slot, not display name."""
from pathlib import Path
import shutil
import subprocess
import unittest


class HudSaveTests(unittest.TestCase):
    def test_explicit_modes_and_same_name_slot_identity(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node is required')
        html=(Path(__file__).resolve().parent.parent/'overlay/hud.html').read_text(encoding='utf-8')
        source=html[html.index('function selectedSave()'):html.index('function updateSaveChoice()')]
        script="""
const elements={sessionSave:{value:'',selectedOptions:[]},sessionSaveName:{value:''}};
const $=key=>elements[key];
"""+source+"""
function check(expected){if(JSON.stringify(selectedSave())!==JSON.stringify(expected))throw Error(JSON.stringify(selectedSave()));}
check(null);
for(const slot of [0,1,9]){
 elements.sessionSave.value='existing:'+slot;
 elements.sessionSave.selectedOptions=[{dataset:{name:'Madeline'}}];
 check({save:'Madeline',create_save:false,save_slot:slot});
}
elements.sessionSave.value='new';elements.sessionSaveName.value='Madeline';
check({save:'Madeline',create_save:true,save_slot:null});
"""
        subprocess.run([node,'-e',script],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()
