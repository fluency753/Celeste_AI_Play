"""Bounded, read-only file presentation for the container's Codex executor.

The worker runs from this trusted source in the shared program sandbox. Only
plain-text results carry KB line receipts; other formats contain real image
payloads for the caller to deliver through the model's image input channel.
Directory listing, glob and grep share the same no-follow path walk, so they
give Codex the non-program equivalents of Claude's Read/Glob/Grep.
"""
from __future__ import annotations

import base64
import errno
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
# Search bounds: one worker call must finish well inside its 90-second sandbox limit.
SEARCH_FILE_BYTES = 8 * 1024 * 1024
SEARCH_TOTAL_BYTES = 256 * 1024 * 1024
SEARCH_ENTRIES = 100000
SEARCH_ROWS = 200000
SEARCH_LIMIT = 250
SEARCH_LINE_CHARS = 2000
MAX_DEPTH = 64
# C0/C1 controls other than tab stay visible as \xNN instead of being rejected or hidden.
_CONTROL = re.compile('[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def _integer(value, name, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum and value > maximum):
        raise ValueError(f'{name} 必须是 1–{maximum} 的整数' if maximum else f'{name} 必须是正整数')
    return value


def _normalize(path, name='file_path'):
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError(f'{name} 必须是非空路径')
    if not path.startswith('/'):
        path = ROOTS[0] + '/' + path
    # Do not normalize away symlink/.. components before the no-follow walk.
    parts = path.split('/')[1:]
    path = '/' + '/'.join(part for part in parts if part and part != '.')
    if '..' in parts or not any(path == root or path.startswith(root + '/') for root in ROOTS):
        raise ValueError('只允许 /workspace、/kb、/sim 内的路径；不允许 .. 路径')
    return path


def _arguments(arguments):
    if not isinstance(arguments, dict):
        raise ValueError('读取参数必须是对象')
    path = arguments.get('file_path', arguments.get('path'))
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('file_path 必须是非空文件路径；旧 path 别名仍可用')
    if 'file_path' in arguments and 'path' in arguments and arguments['file_path'] != arguments['path']:
        raise ValueError('file_path 与旧 path 别名必须完全一致')
    path = _normalize(path)
    offset = _integer(arguments.get('offset', 1), 'offset')
    limit = _integer(arguments.get('limit', 2000), 'limit', maximum=20000)
    return path, offset, limit


def _inside(descriptor):
    # Verify the opened object as well as the requested name. A parent
    # directory moved after opening must not carry us outside the roots.
    actual = os.path.realpath(f'/proc/self/fd/{descriptor}')
    if not any(actual == root or actual.startswith(root + '/') for root in ROOTS):
        raise ValueError('打开后的真实路径不在允许的目录内')
    return actual


def _open(path):
    """Open every component through held dirfds without following symlinks."""
    parts = path.strip('/').split('/')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    try:
        info = os.fstat(target)
        _inside(target)
        return target, info
    except BaseException:
        os.close(target)
        raise


def _read_bytes(path):
    """Walk every component using held dirfds; reject symlinks and hardlinks."""
    target, info = _open(path)
    try:
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(errno.EISDIR, '路径是目录', path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('只能读取普通文件或目录，FIFO、设备不支持')
        if info.st_nlink != 1:
            raise ValueError('不读取硬链接文件；请使用工作区内的普通文件')
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError(f'单文件超过 {MAX_FILE_BYTES // 1024 // 1024} MiB 读取上限')
        with os.fdopen(os.dup(target), 'rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('文件在读取期间超过大小上限')
        return data
    finally:
        os.close(target)


def _decode(data):
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        text = data.decode(encoding)
    except UnicodeError as error:
        raise ValueError('不支持的二进制或文本编码；文本支持 UTF-8/BOM、带 BOM 的 UTF-16') from error
    if '\x00' in text:
        raise ValueError('文件包含 NUL 字节，按二进制文件处理，不能作为纯文本读取')
    return text


def _visible(line):
    """Other control characters are shown as \\xNN so the row stays complete and countable."""
    return _CONTROL.sub(lambda match: f'\\x{ord(match.group()):02x}', line)


def _text_result(path, data, offset, limit):
    # Rows use the same splitlines() boundaries as KbStudy's host-side totals.
    lines = _decode(data).splitlines()
    selected = lines[offset - 1:offset - 1 + limit]
    returned = []
    chars = 0
    for number, line in enumerate(selected, offset):
        displayed = f'{number:6}\t{_visible(line)}\n'
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
        suffix = '\n[该行超过字符上限，仅显示片段，0 个完整行；请用 celeste_grep 定位或在获准的 Bash 中按字符切片。]'
        result.update(text=prefix + _visible(selected[0])[:MAX_CHARS - len(prefix) - len(suffix)] + suffix,
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


def _entry(name, descriptor):
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return f'{_visible(name)}  （无法读取属性）'
    if stat.S_ISLNK(info.st_mode):
        return f'{_visible(name)}  （符号链接，不跟随）'
    if stat.S_ISDIR(info.st_mode):
        return f'{_visible(name)}/'
    if stat.S_ISREG(info.st_mode):
        return f'{_visible(name)}  （{info.st_size} 字节）'
    return f'{_visible(name)}  （特殊文件）'


def _directory_result(path, offset, limit):
    target, info = _open(path)
    try:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('路径不是目录')
        names = sorted(os.listdir(target))
        selected = names[offset - 1:offset - 1 + limit]
        header = f'目录 {path}：共 {len(names)} 项；offset/limit 在此按目录项分页。\n'
        rows, chars = [], len(header)
        for number, name in enumerate(selected, offset):
            row = f'{number:6}\t{_entry(name, target)}\n'
            if chars + len(row) > MAX_CHARS:
                break
            rows.append(row)
            chars += len(row)
    finally:
        os.close(target)
    next_offset = offset + len(rows)
    truncated = next_offset <= len(names)
    result = {'kind': 'directory', 'path': path, 'text': header + ''.join(rows), 'images': [],
              'entries': len(names), 'truncated': truncated, 'next_offset': next_offset if truncated else None}
    if truncated:
        result['notice'] = f'目录项未列完；使用 offset={next_offset} 继续。'
    elif not names:
        result['notice'] = '目录为空。'
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
    try:
        data = _read_bytes(path)
    except IsADirectoryError:
        if arguments.get('pages') is not None:
            raise ValueError('pages 仅用于 PDF；目录使用 offset/limit 按目录项分页') from None
        return _directory_result(path, offset, limit)
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


# ---------------- 目录遍历与搜索（与 Claude 的 Glob/Grep 对应，不运行程序）


class _Scan:
    def __init__(self):
        self.entries = 0
        self.bytes = 0
        self.limited = ''
        self.skipped = {}

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def notice(self):
        parts = [f'{reason} {count} 个' for reason, count in self.skipped.items()]
        text = ('已跳过：' + '、'.join(parts) + '。') if parts else ''
        if self.limited:
            text += f'搜索在{self.limited}后停止，结果可能不完整；请缩小 path 或 glob。'
        return text


def _walk(descriptor, relative, scan, depth=0):
    """Yield regular files under an opened directory; symlinks are listed as skipped, never followed."""
    try:
        names = sorted(os.listdir(descriptor))
    except OSError:
        scan.skip('无法列出的目录')
        return
    for name in names:
        scan.entries += 1
        if scan.entries > SEARCH_ENTRIES:
            scan.limited = f'遍历 {SEARCH_ENTRIES} 个目录项'
            return
        child = f'{relative}/{name}' if relative else name
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            scan.skip('无法读取属性的项')
            continue
        if stat.S_ISLNK(info.st_mode):
            scan.skip('符号链接')
        elif stat.S_ISDIR(info.st_mode):
            if depth >= MAX_DEPTH:
                scan.skip('过深的目录')
                continue
            try:
                opened = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError:
                scan.skip('无法打开的目录')
                continue
            try:
                yield from _walk(opened, child, scan, depth + 1)
            finally:
                os.close(opened)
            if scan.limited:
                return
        elif stat.S_ISREG(info.st_mode):
            yield descriptor, name, child, info
        else:
            scan.skip('特殊文件')


def _glob_body(pattern):
    """Glob → regex body: ** crosses directories, * and ? stay inside one path component."""
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            out.append('(?:.*/)?')
            index += 3
        elif pattern.startswith('**', index):
            out.append('.*')
            index += 2
        elif pattern[index] == '*':
            out.append('[^/]*')
            index += 1
        elif pattern[index] == '?':
            out.append('[^/]')
            index += 1
        elif pattern[index] == '{' and '}' in pattern[index:]:
            end = pattern.index('}', index)
            choices = pattern[index + 1:end].split(',')
            out.append('(?:' + '|'.join(_glob_body(choice) for choice in choices) + ')')
            index = end + 1
        elif pattern[index] == '[' and ']' in pattern[index + 2:]:
            end = pattern.index(']', index + 2)
            body = pattern[index + 1:end].replace('\\', '\\\\')
            out.append('[' + ('^' + body[1:] if body.startswith('!') else body) + ']')
            index = end + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return ''.join(out)


def _glob_regex(pattern):
    try:
        return re.compile(_glob_body(pattern) + r'\Z', re.DOTALL)
    except re.error as error:
        raise ValueError(f'无效的 glob 模式：{error}') from None


def _search_base(arguments):
    if not isinstance(arguments, dict):
        raise ValueError('搜索参数必须是对象')
    pattern = arguments.get('pattern')
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000 or '\x00' in pattern:
        raise ValueError('pattern 必须是 1–1000 字符的字符串')
    base = _normalize(arguments.get('path') or ROOTS[0], 'path')
    offset = _integer(arguments.get('offset', 1), 'offset')
    limit = _integer(arguments.get('limit', SEARCH_LIMIT), 'limit', maximum=20000)
    return pattern, base, offset, limit


def _page(kind, base, header, rows, offset, limit, scan, empty):
    selected = rows[offset - 1:offset - 1 + limit]
    shown, chars = [], len(header)
    for row in selected:
        row = row + '\n'
        if chars + len(row) > MAX_CHARS:
            break
        shown.append(row)
        chars += len(row)
    next_offset = offset + len(shown)
    truncated = next_offset <= len(rows) or bool(scan.limited)
    result = {'kind': kind, 'path': base, 'text': header + ''.join(shown), 'images': [], 'matches': len(rows),
              'truncated': truncated, 'next_offset': next_offset if next_offset <= len(rows) else None}
    notices = []
    if next_offset <= len(rows):
        notices.append(f'结果未列完；使用 offset={next_offset} 继续。')
    elif not rows:
        notices.append(empty)
    if scan.notice():
        notices.append(scan.notice())
    if notices:
        result['notice'] = ''.join(notices)
    return result


def _glob(arguments):
    if isinstance(arguments, dict) and isinstance(arguments.get('pattern'), str) and arguments['pattern'].startswith('/'):
        # An absolute pattern names its own base: the literal directory prefix.
        literal, pieces = [], arguments['pattern'].split('/')[1:]
        while len(pieces) > 1 and not any(char in pieces[0] for char in '*?[{'):
            literal.append(pieces.pop(0))
        arguments = {**arguments, 'path': _normalize('/' + '/'.join(literal), 'pattern'), 'pattern': '/'.join(pieces)}
    pattern, base, offset, limit = _search_base(arguments)
    matcher = _glob_regex(pattern)
    scan = _Scan()
    target, info = _open(base)
    try:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('celeste_glob 的 path 必须是目录')
        found = [(entry.st_mtime, f'{base}/{relative}') for _, _, relative, entry in _walk(target, '', scan)
                 if matcher.match(relative)]
    finally:
        os.close(target)
    found.sort(key=lambda item: (-item[0], item[1]))
    rows = [_visible(path) for _, path in found]
    header = f'在 {base} 中匹配 {pattern}：共 {len(rows)} 个文件（按修改时间从新到旧）。\n'
    return _page('glob', base, header, rows, offset, limit, scan, '没有匹配的文件。')


def _search_text(descriptor, name, info, scan):
    """Read one regular file for grep without following links; None means skipped."""
    if info.st_nlink != 1:
        scan.skip('硬链接文件')
        return None
    if info.st_size > SEARCH_FILE_BYTES:
        scan.skip(f'超过 {SEARCH_FILE_BYTES // 1024 // 1024} MiB 的文件')
        return None
    if scan.bytes + info.st_size > SEARCH_TOTAL_BYTES:
        scan.limited = f'读取 {SEARCH_TOTAL_BYTES // 1024 // 1024} MiB'
        return None
    try:
        handle = (os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                  if name is not None else os.dup(descriptor))
    except OSError:
        scan.skip('无法打开的文件')
        return None
    with os.fdopen(handle, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            scan.skip('读取时已变化的文件')
            return None
        data = stream.read(SEARCH_FILE_BYTES + 1)
    scan.bytes += len(data)
    try:
        return _decode(data)
    except ValueError:
        scan.skip('二进制或不支持编码的文件')
        return None


def _grep(arguments):
    pattern, base, offset, limit = _search_base(arguments)
    mode = arguments.get('output_mode', 'files_with_matches')
    if mode not in {'files_with_matches', 'content', 'count'}:
        raise ValueError('output_mode 只能是 files_with_matches、content 或 count')
    context = arguments.get('context', 0)
    if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 20:
        raise ValueError('context 必须是 0–20 的整数')
    if not isinstance(arguments.get('ignore_case', False), bool):
        raise ValueError('ignore_case 必须是布尔值')
    try:
        matcher = re.compile(pattern, re.IGNORECASE if arguments.get('ignore_case') else 0)
    except re.error as error:
        raise ValueError(f'pattern 不是有效的 Python 正则表达式：{error}') from None
    names = arguments.get('glob')
    if names is not None and (not isinstance(names, str) or not names or len(names) > 1000):
        raise ValueError('glob 必须是非空文件名模式，例如 "*.md" 或 "**/*.py"')
    selector = _glob_regex(names) if names else None
    scan = _Scan()
    found, rows, files = [], [], ()
    target, info = _open(base)
    try:
        if stat.S_ISREG(info.st_mode):
            files = [(target, None, Path(base).name, info)]
            base_dir = str(Path(base).parent)
        elif stat.S_ISDIR(info.st_mode):
            files = _walk(target, '', scan)
            base_dir = base
        else:
            raise ValueError('path 必须是普通文件或目录')
        for descriptor, name, relative, entry in files:
            if selector and not selector.match(relative if '/' in names else relative.rsplit('/', 1)[-1]):
                continue
            text = _search_text(descriptor, name, entry, scan)
            if scan.limited:
                break
            if text is None:
                continue
            lines = text.splitlines()
            hits = {index for index, line in enumerate(lines) if matcher.search(line)}
            if not hits:
                continue
            path = _visible(f'{base_dir}/{relative}')
            found.append((entry.st_mtime, path, len(hits)))
            if mode == 'content':
                shown, last = set(), None
                for index in sorted(hits):
                    for row in range(max(0, index - context), min(len(lines), index + context + 1)):
                        if row in shown:
                            continue
                        if last is not None and row > last + 1:
                            rows.append('--')
                        shown.add(row)
                        last = row
                        line = _visible(lines[row])
                        if len(line) > SEARCH_LINE_CHARS:
                            line = line[:SEARCH_LINE_CHARS] + '…［该行过长，已截断］'
                        rows.append(f'{path}{":" if row in hits else "-"}{row + 1}{":" if row in hits else "-"}{line}')
                rows.append('--')
            if len(rows) > SEARCH_ROWS:
                scan.limited = f'收集 {SEARCH_ROWS} 行结果'
                break
    finally:
        if hasattr(files, 'close'):
            files.close()
        os.close(target)
    if mode == 'content':
        while rows and rows[-1] == '--':
            rows.pop()
    elif mode == 'count':
        rows = [f'{path}:{count}' for _, path, count in found]
    else:
        rows = [path for _, path in sorted(((-mtime, path) for mtime, path, _ in found))]
    header = (f'在 {base} 中搜索 {pattern!r}（{mode}）：{len(found)} 个文件有匹配。\n')
    return _page('grep', base, header, rows, offset, limit, scan, '没有匹配的内容。')


def _dispatch(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('arguments'), dict):
        raise ValueError('工作器请求必须包含 operation 与 arguments 对象')
    operation = payload.get('operation')
    if operation == 'read':
        return _read(payload['arguments'])
    if operation == 'glob':
        return _glob(payload['arguments'])
    if operation == 'grep':
        return _grep(payload['arguments'])
    raise ValueError('未知的只读文件操作')


def _serve(payload):
    try:
        result = _dispatch(payload)
    except Exception as error:  # Always answer with a structured, non-receipt error.
        message = str(error) or type(error).__name__
        result = {'kind': 'error', 'error': message, 'text': f'读取失败：{message}', 'images': []}
    print(json.dumps(result, ensure_ascii=False))


def _worker_main():
    try:
        payload = json.load(__import__('sys').stdin)
    except ValueError:
        payload = None
    _serve(payload)


# The worker source travels on stdin: as a command-line argument it would approach
# Windows' 32767-character limit for the docker.exe invocation.
BOOTSTRAP = ("import json,sys\n"
             "request=json.loads(sys.stdin.read())\n"
             "scope={'__name__':'celeste_read_worker'}\n"
             "exec(compile(request['source'],'codex_read.py','exec'),scope)\n"
             "scope['_serve'](request['payload'])\n")


def _run_worker(container: str, operation: str, arguments: dict) -> dict:
    from .codex_container import _run
    from .program_sandbox import bwrap_args
    sandbox = bwrap_args() + ['--ro-bind', '/workspace', '/workspace', '--tmpfs', '/tmp']
    request = {'source': Path(__file__).read_text(encoding='utf-8'),
               'payload': {'operation': operation, 'arguments': arguments}}
    result = _run(container, '/usr/bin/timeout', '--signal=TERM', '--kill-after=2s', '90s',
                  *sandbox, '/opt/py/bin/python', '-I', '-c', BOOTSTRAP,
                  input=json.dumps(request, ensure_ascii=False), timeout=95)
    if result.returncode:
        raise ValueError('只读文件工作器失败或超时；未返回可用内容。' + result.stderr.strip()[-500:])
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError('只读文件工作器未返回有效结果') from error
    if not isinstance(payload, dict):
        raise ValueError('只读文件工作器结果必须是对象')
    return payload


def read_file(container: str, arguments: dict) -> dict:
    """Present a container file or directory without granting native program or write access."""
    return _run_worker(container, 'read', arguments)


def search_files(container: str, operation: str, arguments: dict) -> dict:
    """celeste_glob / celeste_grep: the same read-only worker, roots and no-follow walk."""
    if operation not in {'glob', 'grep'}:
        raise ValueError('未知的搜索操作')
    return _run_worker(container, operation, arguments)


if __name__ == '__main__':
    _worker_main()
