"""客户端文件工具：可信 Python 帮手在容器内执行，检查真实路径，返回实际提供的行范围。"""
from __future__ import annotations

import json
import subprocess

from .config import find_docker_exe


WORKER = r'''
import json,sys
from pathlib import Path
a=json.load(sys.stdin)
p=Path(a['path'])
if not p.is_absolute():p=Path('/workspace')/p
p=p.resolve()
allowed=[Path('/workspace'),Path('/kb'),Path('/sim')]
if not any(p==root or root in p.parents for root in allowed):raise ValueError('路径不在 /workspace、/kb 或 /sim 内')
if a['operation']=='write':
 if Path('/workspace') not in p.parents:raise ValueError('只能写入 /workspace')
 text=a['content']
 if len(text)>200000:raise ValueError('单次写入超过 200000 字符')
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_text(text,encoding='utf-8')
 print(json.dumps({'path':str(p),'written':len(text)}))
else:
 lines=p.read_text(encoding='utf-8').splitlines()
 offset=int(a.get('offset',1));limit=int(a.get('limit',200))
 if offset<1 or not 1<=limit<=500:raise ValueError('offset >= 1，limit 为 1–500')
 out=[];size=0
 for line in lines[offset-1:offset-1+limit]:
  if size+len(line)+1>24000:break
  out.append(line);size+=len(line)+1
 if not out and offset<=len(lines):raise ValueError('该行过长，无法完整返回；未计入阅读进度')
 print(json.dumps({'path':str(p),'startLine':offset,'numLines':len(out),'totalLines':len(lines),'text':'\n'.join(out)},ensure_ascii=False))
'''


def workspace_call(container: str, operation: str, arguments: dict) -> dict:
    result = subprocess.run([find_docker_exe(), "exec", "-i", "-u", "agent", container, "python3", "-c", WORKER],
                            input=json.dumps({**arguments, "operation": operation}), capture_output=True,
                            encoding="utf-8", timeout=20)
    if result.returncode:
        raise ValueError(result.stderr.strip()[-1500:])
    return json.loads(result.stdout)
