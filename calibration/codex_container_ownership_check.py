"""Explicit isolated Docker check; no model, account, game or controller is used."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import uuid

from orchestrator import codex_container as codex
from orchestrator.container import docker


FIXTURE = r'''
import json,os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
worker=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'],start_new_session=True)
print(json.dumps({'server':os.getpid(),'worker':worker.pid}),flush=True)
while True:time.sleep(.1)
'''


async def check(image):
    suffix = uuid.uuid4().hex[:12]
    name, volume = 'codex-owner-'+suffix, 'celeste-codex-owner-'+suffix
    label = 'codex-owned-process-check'
    processes = []

    def owned(kind, target):
        field = '{{index .Config.Labels "celeste-ai-play.test"}}' if kind == 'container' else '{{index .Labels "celeste-ai-play.test"}}'
        result = docker(kind,'inspect','-f',field,target,check=False)
        return result.returncode == 0 and result.stdout.strip() == label

    def alive(pids):
        result = docker('exec',name,'python3','-c',
                        "import json,sys;from pathlib import Path;print(json.dumps([int(p) for p in sys.argv[1:] if Path('/proc',p).exists() and Path('/proc',p,'stat').read_text().split(') ')[1][0]!='Z']))", *map(str,pids))
        return json.loads(result.stdout)

    async def launch(owner):
        proc = await asyncio.create_subprocess_exec(*codex.launch_command(name,owner,'python3','-c',FIXTURE),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        processes.append(proc)
        line = await asyncio.wait_for(proc.stdout.readline(),5)
        if not line:
            raise AssertionError((await proc.stderr.read()).decode())
        return proc,json.loads(line)

    try:
        docker('volume','create','--label','celeste-ai-play.test='+label,volume)
        docker('run','-d','--name',name,'--network','none','--user','agent',
               '--label','celeste-ai-play.test='+label,'-v',volume+':'+codex.CODEX_HOME,
               '--entrypoint','sleep',image,'infinity')
        docker('exec','-d','-u','agent',name,'python3','-c',
               "import os,time;from pathlib import Path;Path('/tmp/unrelated.pid').write_text(str(os.getpid()));time.sleep(120)")
        for _ in range(50):
            pid = docker('exec',name,'cat','/tmp/unrelated.pid',check=False)
            if pid.returncode == 0:
                unrelated = int(pid.stdout)
                break
            await asyncio.sleep(.05)
        else:
            raise AssertionError('Unrelated fixture did not start')

        proc, pids = await launch(uuid.uuid4().hex)
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(),10)
        assert not alive(pids.values()), pids
        assert alive([unrelated]) == [unrelated]

        owner = uuid.uuid4().hex
        proc,pids = await launch(owner)
        await asyncio.to_thread(codex.terminate_owned_server,name,owner)
        await asyncio.wait_for(proc.wait(),5)
        assert not alive(pids.values()), pids
        assert alive([unrelated]) == [unrelated]
        await asyncio.to_thread(codex.terminate_owned_server,name,owner)

        owner = uuid.uuid4().hex
        proc,pids = await launch(owner)
        duplicate = await asyncio.create_subprocess_exec(*codex.launch_command(name,owner,'python3','-c','import time;time.sleep(120)'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        processes.append(duplicate)
        assert await asyncio.wait_for(duplicate.wait(),5) != 0
        await asyncio.to_thread(codex.terminate_owned_server,name,owner)
        await asyncio.wait_for(proc.wait(),5)
        assert not alive(pids.values()), pids
        assert alive([unrelated]) == [unrelated]
        stale = uuid.uuid4().hex
        docker('exec','-e','CODEX_HOME='+codex.CODEX_HOME,name,'python3','-c',
            "import json,os,sys;from pathlib import Path;r={'pid':int(sys.argv[2]),'start':'not-the-start-time','uid':os.getuid()};p=Path(os.environ['CODEX_HOME'])/'hosts'/(sys.argv[1]+'.json');p.write_text(json.dumps({'owner':sys.argv[1],'uid':os.getuid(),'launcher':r,'server':r}))", stale,str(unrelated))
        await asyncio.to_thread(codex.terminate_owned_server,name,stale)
        assert alive([unrelated]) == [unrelated]
        return {'passed':True,'image':image,'checks':['stdin EOF kills owned nested separate-session descendants',
            'remote termination kills owned descendants','repeated termination is idempotent',
            'duplicate owner cannot erase active owner marker','stale PID/start-time marker cannot kill reused PID',
            'unrelated same-UID process survives'],
            'boundary':'Dedicated network-none Docker container; synthetic long-running process tree; no model/auth/game/controller'}
    finally:
        if owned('container',name):
            docker('rm','-f',name)
        for proc in processes:
            if proc.returncode is None:
                await asyncio.wait_for(proc.wait(),5)
        if owned('volume',volume):
            docker('volume','rm',volume)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='celeste-agent:codex-native-dev')
    parser.add_argument('--out',type=Path,default=Path('local/codex-container-ownership.json'))
    args = parser.parse_args()
    result = asyncio.run(check(args.image))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
