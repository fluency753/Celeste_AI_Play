"""Bounded, read-only file presentation for the container's Codex executor.

The worker runs from this trusted source in the shared program sandbox. Only
plain-text results carry KB line receipts; other formats contain real image
payloads for the caller to deliver through the model's image input channel.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


MAX_CHARS = 100000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGES_BYTES = 4 * MAX_IMAGE_BYTES
MAX_IMAGES = 20
ROOTS = ('/workspace', '/kb', '/sim')


def _integer(value, name, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum and value > maximum):
        raise ValueError(f'{name} 必须是 1–{maximum} 的整数' if maximum else f'{name} 必须是正整数')
    return value


def _arguments(arguments):
    if not isinstance(arguments, dict):
        raise ValueError('读取参数必须是对象')
    path = arguments.get('file_path', arguments.get('path'))
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('file_path 必须是非空文件路径；旧 path 别名仍可用')
    if 'file_path' in arguments and 'path' in arguments and arguments['file_path'] != arguments['path']:
        raise ValueError('file_path 与旧 path 别名必须完全一致')
    if not path.startswith('/'):
        path = '/workspace/' + path
    # Do not normalize away symlink/.. components before the no-follow walk.
    parts = path.split('/')[1:]
    if not parts or parts[0] not in {'workspace', 'kb', 'sim'} or '..' in parts:
        raise ValueError('只允许 /workspace、/kb、/sim 内文件；不允许 .. 路径')
    path = '/' + '/'.join(part for part in parts if part and part != '.')
    offset = _integer(arguments.get('offset', 1), 'offset')
    limit = _integer(arguments.get('limit', 2000), 'limit', maximum=20000)
    return path, offset, limit


def _read_bytes(path):
    """Walk every component using held dirfds; reject symlinks and hardlinks."""
    parts = path.strip('/').split('/')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            info = os.fstat(target)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('只能读取普通文件，目录、FIFO、设备不支持')
            if info.st_nlink != 1:
                raise ValueError('不读取硬链接文件；请使用工作区内的普通文件')
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError(f'单文件超过 {MAX_FILE_BYTES // 1024 // 1024} MiB 读取上限')
            # Verify the opened object as well as the requested name. A parent
            # directory moved after opening must not carry us outside the roots.
            actual = os.path.realpath(f'/proc/self/fd/{target}')
            if not any(actual.startswith(root + '/') for root in ROOTS):
                raise ValueError('打开后的真实路径不在允许的目录内')
            with os.fdopen(os.dup(target), 'rb') as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError('文件在读取期间超过大小上限')
            return data
        finally:
            os.close(target)
    finally:
        os.close(descriptor)


def _decode(data):
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        text = data.decode(encoding)
    except UnicodeError as error:
        raise ValueError('不支持的二进制或文本编码；文本支持 UTF-8/BOM、带 BOM 的 UTF-16') from error
    if any(ord(char) < 32 and char not in '\n\r\t\f' for char in text):
        raise ValueError('文件包含二进制控制字符，不能作为纯文本读取')
    return text


def _text_result(path, data, offset, limit):
    lines = _decode(data).splitlines()
    selected = lines[offset - 1:offset - 1 + limit]
    returned = []
    chars = 0
    for number, line in enumerate(selected, offset):
        displayed = f'{number:6}\t{line}\n'
        if chars + len(displayed) > MAX_CHARS:
            break
        returned.append(displayed)
        chars += len(displayed)
    count = len(returned)
    next_offset = offset + count
    truncated = next_offset <= len(lines)
    result = {'kind': 'text', 'path': path, 'text': ''.join(returned),
              'file': {'filePath': path, 'startLine': offset, 'numLines': count, 'totalLines': len(lines)},
              'images': [], 'truncated': truncated, 'next_offset': next_offset if truncated else None}
    if selected and not returned:
        prefix = f'{offset:6}\t'
        suffix = '\n[该行超过字符上限，仅显示片段，0 个完整行；请用原生 Shell 按字符切片或先格式化到另一文件后读取。]'
        result.update(text=prefix + selected[0][:MAX_CHARS - len(prefix) - len(suffix)] + suffix,
                      error='单行过长，未返回完整行，不计入 KB 阅读进度', next_offset=None)
    elif truncated:
        # Keep diagnostic metadata separate so displayed text stays within its
        # 100000-character budget, including the visible line numbers.
        result['notice'] = f'输出已截断；使用 offset={next_offset} 继续读取。'
    elif offset > len(lines) and lines:
        result['notice'] = f'offset={offset} 已超过文件末尾（共 {len(lines)} 行）。'
    elif not lines:
        result['notice'] = '文件存在，但内容为空（共 0 行）。'
    return result


def _image(data):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as original:
        if original.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'}:
            raise ValueError('图像只支持 PNG、JPEG、GIF、WebP')
        if original.width * original.height > 40000000:
            raise ValueError('图像超过 4000 万像素上限')
        image = ImageOps.exif_transpose(original)
        image.thumbnail((1280, 1280))
        if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, 'white')
            background.paste(rgba, mask=rgba.getchannel('A'))
            image = background
        else:
            image = image.convert('RGB')
        while True:
            out = io.BytesIO()
            image.save(out, format='JPEG', quality=85)
            encoded = out.getvalue()
            if len(encoded) <= MAX_IMAGE_BYTES:
                return {'mimeType': 'image/jpeg', 'data': base64.b64encode(encoded).decode('ascii')}
            image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)))


class _Presentation:
    def __init__(self, kind, path):
        self.result = {'kind': kind, 'path': path, 'text': '', 'images': [],
                       'truncated': False, 'next_offset': None}
        self.image_bytes = 0

    def text(self, value):
        remaining = MAX_CHARS - len(self.result['text'])
        self.result['text'] += value[:remaining]
        if len(value) > remaining:
            self.result['truncated'] = True
            self.result['notice'] = '文本超过 100000 字符预算，后续内容未显示；请缩小读取范围。'

    def image(self, data):
        if len(self.result['images']) >= MAX_IMAGES:
            self.result['truncated'] = True
            return False
        rendered = _image(data)
        size = len(base64.b64decode(rendered['data']))
        if self.image_bytes + size > MAX_IMAGES_BYTES:
            self.result['truncated'] = True
            return False
        self.result['images'].append(rendered)
        self.image_bytes += size
        return True


def _pages(value, total):
    if value is None:
        return list(range(1, min(total, 5) + 1))
    if not isinstance(value, str) or len(value) > 120:
        raise ValueError('pages 必须是页码范围字符串，例如 "1-5"；每次最多 20 页')
    pages = []
    for piece in value.split(','):
        match = re.fullmatch(r'\s*([1-9][0-9]{0,5})(?:-([1-9][0-9]{0,5}))?\s*', piece)
        if not match:
            raise ValueError('pages 仅支持正整数页码或范围，例如 "1-5,8"')
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last or last > total or last - first >= 20:
            raise ValueError(f'页码超出范围；PDF 共 {total} 页，每次最多 20 页')
        pages.extend(range(first, last + 1))
        pages = list(dict.fromkeys(pages))
        if len(pages) > 20:
            raise ValueError('每次最多读取 20 个 PDF 页面')
    return pages


def _poppler(command):
    def limits():
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = subprocess.run(command, stdout=output, stderr=error, timeout=15, preexec_fn=limits)
        if process.returncode:
            raise ValueError('PDF 解析失败：文件可能损坏、加密或超出资源上限')
        output.seek(0)
        return output.read(16 * 1024 * 1024 + 1)


def _pdf_result(path, data, pages):
    presentation = _Presentation('pdf', path)
    with tempfile.TemporaryDirectory() as folder:
        document = str(Path(folder) / 'document.pdf')
        Path(document).write_bytes(data)
        info = _poppler(['/usr/bin/pdfinfo', document]).decode('utf-8', errors='replace')
        count = re.search(r'^Pages:\s+(\d+)\s*$', info, re.MULTILINE)
        if not count:
            raise ValueError('无法确定 PDF 总页数')
        total = int(count[1])
        if total < 1:
            raise ValueError('PDF 没有可读取的页面')
        chosen = _pages(pages, total)
        returned = []
        text_pages = []
        presentation.text(f'PDF 共 {total} 页；本次页码：{", ".join(map(str, chosen))}。\n')
        for page in chosen:
            arguments = ['-f', str(page), '-l', str(page)]
            text = _poppler(['/usr/bin/pdftotext', *arguments, '-layout', '-enc', 'UTF-8', document, '-'])
            presentation.text(f'\n--- 第 {page} 页 ---\n' + text.decode('utf-8', errors='replace'))
            text_pages.append(page)
            bitmap = _poppler(['/usr/bin/pdftoppm', *arguments, '-scale-to', '1280',
                               '-singlefile', '-png', document])
            if not presentation.image(bitmap):
                presentation.result['notice'] = f'图像预算已满；第 {page} 页及后续页图像未返回，请缩小 pages 范围。'
                break
            returned.append(page)
        presentation.result['total_pages'] = total
        presentation.result['requested_pages'] = chosen
        presentation.result['pages'] = returned
        presentation.result['text_pages'] = text_pages
    return presentation.result


def _notebook_result(path, data, offset, limit):
    notebook = json.loads(_decode(data))
    if not isinstance(notebook, dict) or not isinstance(notebook.get('cells'), list):
        raise ValueError('不是有效的 Jupyter Notebook（缺少 cells 数组）')
    cells = notebook['cells']
    presentation = _Presentation('notebook', path)
    presentation.text(f'Notebook 共 {len(cells)} 个单元；offset/limit 在此按单元编号分页。\n')
    for number, cell in enumerate(cells[offset - 1:offset - 1 + limit], offset):
        if not isinstance(cell, dict):
            raise ValueError('Notebook 单元必须是对象')
        presentation.text(f'\n--- 单元 {number} ({cell.get("cell_type", "unknown")}) ---\n')
        source = cell.get('source', '')
        if isinstance(source, list):
            source = ''.join(part for part in source if isinstance(part, str))
        if isinstance(source, str):
            presentation.text(source + '\n')
        outputs = cell.get('outputs', [])
        if not isinstance(outputs, list):
            raise ValueError('Notebook outputs 必须是数组')
        for output in outputs:
            if presentation.result['truncated']:
                break
            if not isinstance(output, dict):
                continue
            value = output.get('text', '')
            if isinstance(value, list):
                value = ''.join(part for part in value if isinstance(part, str))
            if isinstance(value, str):
                presentation.text(value)
            traceback = output.get('traceback')
            if isinstance(traceback, list):
                presentation.text('\n'.join(part for part in traceback if isinstance(part, str)) + '\n')
            bundle = output.get('data') or {}
            if not isinstance(bundle, dict):
                continue
            value = bundle.get('text/plain', '')
            if isinstance(value, list):
                value = ''.join(part for part in value if isinstance(part, str))
            if isinstance(value, str):
                presentation.text(value + '\n')
            for mime in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                value = bundle.get(mime)
                if isinstance(value, list):
                    value = ''.join(part for part in value if isinstance(part, str))
                if isinstance(value, str):
                    try:
                        presentation.image(base64.b64decode(value, validate=True))
                    except (ValueError, OSError) as error:
                        presentation.text(f'\n[单元 {number} 的图像无法读取：{type(error).__name__}]\n')
                    break
        if presentation.result['truncated']:
            presentation.result['next_offset'] = number
            presentation.text('\n[输出预算已满；请缩小 limit，单独读取当前单元。]\n')
            break
    else:
        following = offset + min(limit, max(0, len(cells) - offset + 1))
        if following <= len(cells):
            presentation.result.update(truncated=True, next_offset=following)
    return presentation.result


def _read(arguments):
    path, offset, limit = _arguments(arguments)
    data = _read_bytes(path)
    suffix = Path(path).suffix.lower()
    is_pdf = data.startswith(b'%PDF-') or suffix == '.pdf'
    if arguments.get('pages') is not None and not is_pdf:
        raise ValueError('pages 仅用于 PDF；文本使用 offset/limit 按行分页')
    if is_pdf:
        return _pdf_result(path, data, arguments.get('pages'))
    image_magic = (data.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a'))
                   or (data.startswith(b'RIFF') and data[8:12] == b'WEBP'))
    if image_magic or suffix in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
        presentation = _Presentation('image', path)
        presentation.image(data)
        presentation.text('图像已缩放至最长边 1280 像素；动画仅显示首帧，透明背景合成为白色。')
        return presentation.result
    if suffix == '.ipynb':
        return _notebook_result(path, data, offset, limit)
    return _text_result(path, data, offset, limit)


def _worker_main():
    try:
        arguments = json.load(__import__('sys').stdin)
        result = _read(arguments)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result = {'kind': 'error', 'error': str(error), 'text': f'读取失败：{error}', 'images': []}
    print(json.dumps(result, ensure_ascii=False))


def read_file(container: str, arguments: dict) -> dict:
    """Present a container file without granting native program or write access."""
    from .codex_container import _run
    from .program_sandbox import bwrap_args
    sandbox = bwrap_args() + ['--ro-bind', '/workspace', '/workspace', '--tmpfs', '/tmp']
    source = Path(__file__).read_text(encoding='utf-8')
    result = _run(container, '/usr/bin/timeout', '--signal=TERM', '--kill-after=2s', '90s',
                  *sandbox, '/opt/py/bin/python', '-I', '-c', source,
                  input=json.dumps(arguments, ensure_ascii=False), timeout=95)
    if result.returncode:
        raise ValueError('只读文件工作器失败或超时；未返回可用内容。' + result.stderr.strip()[-500:])
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError('只读文件工作器未返回有效结果') from error
    if not isinstance(payload, dict):
        raise ValueError('只读文件工作器结果必须是对象')
    return payload


if __name__ == '__main__':
    _worker_main()
