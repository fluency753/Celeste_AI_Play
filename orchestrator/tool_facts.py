"""Bounded public tool summaries shared by execution and HUD; never result bodies."""
import json
import re


def safe_text(value, limit=700):
    text = str(value)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[隐藏]', text)
    text = re.sub(r'[A-Za-z0-9+/=]{160,}', '[编码省略]', text)
    text = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|password|authorization)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1[隐藏]', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def request_summary(name, args):
    name = str(name or 'unknown').removeprefix('mcp__celeste__').removeprefix('celeste_')
    args = args if isinstance(args, dict) else {}
    allowed = ('detail', 'region', 'offset', 'limit', 'snapshot_id', 'path', 'file_path', 'plan_file',
               'action', 'choice', 'stop_on', 'input_format', 'timeout_s', 'description', 'command',
               'cwd', 'workdir', 'inputs', 'candidates', 'pattern', 'glob', 'include', 'output_mode', 'effort', 'reason')
    params = {key: args[key] for key in allowed if key in args}
    # Everything is display-only, with per-field and whole-record budgets.
    params = {key: safe_text(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                             1200 if key == 'command' else 240) for key, value in params.items()}
    detail = args.get('detail', 'overview')
    observation = ({'overview': '完整房间布局', 'geometry': '区域碰撞精查', 'player': '完整玩家状态'}.get(detail, '观测（未知 detail）')
                   if isinstance(detail, str) else '观测（detail 参数异常）')
    label = {'observe': observation,
             'Read': '读取文件', 'read': '读取文件', 'Write': '写文件', 'write': '写文件',
             'Bash': '执行程序', 'bash': '执行程序', 'apply_patch': '编辑文件', 'view_image': '查看图片', 'act': '游戏动作', 'simulate': '模拟预测',
             'cutscene': '剧情操作', 'narrate': '公开解说', 'set_effort': '调整推理强度',
             'stop_run': '报告阻塞', 'finish_goal': '报告目标达成', 'exit_level': '退出关卡'}.get(name, name)
    if name.lower() in {'read', 'write'}:
        label += ' · ' + safe_text(args.get('path') or args.get('file_path') or '路径未知', 150)
    if name == 'act':
        inputs = args.get('inputs')
        if isinstance(inputs, list):
            params['输入段数'] = len(inputs)
            if len(inputs) <= 2048 and all(isinstance(v, str) and re.fullmatch(r'[0-9]{1,12}', v.split(',')[0].strip()) for v in inputs):
                params['计划帧数'] = sum(int(v.split(',')[0]) for v in inputs)
    if name == 'simulate' and isinstance(args.get('candidates'), list):
        params['候选条数'] = len(args['candidates'])
    if name.lower() == 'write' and isinstance(args.get('content'), str):
        params['写入字符数'] = len(args['content'])
    return safe_text(label, 220), params


def result_summary(content, success):
    texts, images = [], 0
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') in {'text', 'inputText', 'input_text'}:
                if isinstance(block.get('text'), str): texts.append(block['text'])
            elif block.get('type') in {'image', 'inputImage', 'input_image'}:
                images += 1
    result = {'文字字符数': sum(len(t) for t in texts) if content is not None else None,
              '工具结果图片数': images if content is not None else None}
    for text in texts:
        if len(text) > 32000:
            continue
        candidate = text
        for marker in ('当前帧的统一碰撞数据（变化）：\n', '后续按快照返回变化）：\n'):
            if marker in text:
                candidate = text.split(marker, 1)[1]
        try:
            data = json.JSONDecoder().raw_decode(candidate)[0]
            if isinstance(data, dict):
                data = data['collision'] if isinstance(data.get('collision'), dict) else data
                for key, label in [('frame', '帧号'), ('room', '房间'), ('mode', '几何返回'),
                                   ('object_count', '对象数'), ('matched_total', '区域匹配数'),
                                   ('returned', '本页对象数'), ('returncode', '程序退出码'), ('timed_out', '程序超时')]:
                    if key in data and isinstance(data[key], (str, bool, int, float)):
                        result[label] = safe_text(data[key], 100)
        except (ValueError, TypeError):
            pass
    return result

