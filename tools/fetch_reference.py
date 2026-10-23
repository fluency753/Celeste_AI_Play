"""把两份外部资料原文抓取到知识库的 reference/ 目录（只放本机，不提交到仓库）。

1. Celeste Wiki（celeste.ink）"Tech" 页面及其分类下的全部技巧子页面：通过 MediaWiki API 取渲染后的页面，
   转成 Markdown，图片与视频保留文件名占位。
2. Strawberry Jam（草莓酱）训练场（Beginner → Grandmaster 各房间的说明文字）与机制库（Library）的全部文本，
   直接从本机安装的 Mod 压缩包里的对话文件读取，英文与简体中文各一份，去掉显示用的格式代码。

用法（仓库根目录，需要能访问 celeste.ink；草莓酱部分需要本机装有该 Mod）：
    python tools/fetch_reference.py --celeste-dir "D:\\path\\to\\steamapps\\common\\Celeste"

输出（kb/reference/ 已在 .gitignore 中，随 kb/ 一起只读挂载进智能体容器的 /kb/reference/）：
    kb/reference/README.md
    kb/reference/celeste_wiki/<页面>.md
    kb/reference/strawberry_jam/gyms_en.md、gyms_zh.md、library_en.md、library_zh.md
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_API = "https://celeste.ink/w/api.php"
WIKI_BASE = "https://celeste.ink/wiki/"
USER_AGENT = "Celeste_AI_Play reference fetcher (https://github.com/fluency753/Celeste_AI_Play)"
WIKI_EXCLUDE = re.compile(r"/sandbox$", re.IGNORECASE)  # 草稿页
SJ_ZIP = "StrawberryJam2021.zip"
SJ_LANGS = {"en": "Dialog/English.txt", "zh": "Dialog/Simplified Chinese.txt"}
SJ_LEVELS = ["Beginner", "Intermediate", "Advanced", "Expert", "Grandmaster"]


# ---------------------------------------------------------------- Celeste Wiki

def api(params: dict) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    req = urllib.request.Request(f"{WIKI_API}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"wiki API 出错：{data['error']}")
    time.sleep(0.5)  # 对站点友好一些
    return data


def tech_pages() -> list[str]:
    titles = ["Tech"]
    params = {"action": "query", "list": "categorymembers", "cmtitle": "Category:Tech", "cmnamespace": "0",
              "cmlimit": "500"}
    while True:
        data = api(params)
        for member in data["query"]["categorymembers"]:
            title = member["title"]
            if title not in titles and not WIKI_EXCLUDE.search(title):
                titles.append(title)
        if "continue" not in data:
            break
        params.update(data["continue"])
    # "Tech/" 下的子页面（例如从其他 wiki 移植来的技巧难度表），有的没有挂分类
    data = api({"action": "query", "list": "allpages", "apprefix": "Tech/", "apnamespace": "0", "aplimit": "500"})
    for page in data["query"]["allpages"]:
        if page["title"] not in titles and not WIKI_EXCLUDE.search(page["title"]):
            titles.append(page["title"])
    return titles


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def cls(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


VOID = {"br", "img", "hr", "meta", "link", "input", "source", "track", "wbr", "col", "area", "base", "param"}


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


SKIP_CLASSES = {"mw-editsection", "toc", "navbox", "mw-empty-elt", "noprint", "tabber__header", "mw-cite-backlink",
                "printfooter", "catlinks", "mwe-math-mathml-inline", "mwe-math-mathml-display"}
BLOCK_TAGS = {"p", "div", "section", "article", "figure", "blockquote", "center", "main", "header", "footer", "aside",
              "figcaption", "dl", "table", "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "gallery"}


def media_name(node: Node) -> str:
    for key in ("data-mwtitle", "alt", "src"):
        value = node.attrs.get(key)
        if value:
            if key == "src":
                value = urllib.parse.unquote(value.rsplit("/", 1)[-1])
                value = re.sub(r"^\d+px-", "", value)
            return value
    return "?"


class Renderer:
    """把 MediaWiki 渲染出的 HTML 转成 Markdown（只保留文字结构；图片、视频写成文件名占位）。"""

    def __init__(self):
        self.media: list[str] = []

    # 行内内容：返回一段不含块级空行的文字
    def inline(self, node: Node | str, pre: bool = False) -> str:
        if isinstance(node, str):
            return node if pre else re.sub(r"\s+", " ", node)
        if self.skipped(node):
            return ""
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag == "img":
            if "mwe-math-fallback-image-inline" in node.cls() or "mwe-math-fallback-image-display" in node.cls():
                alt = node.attrs.get("alt", "").strip()
                alt = re.sub(r"^\{\\(?:display|text)style\s*(.*)\}$", r"\1", alt, flags=re.S).strip()
                return f"${alt}$"
            name = media_name(node)
            self.media.append(name)
            return f"［图：{name}］"
        if tag in ("video", "audio"):
            name = media_name(node)
            self.media.append(name)
            return f"［视频：{name}］"
        if tag == "math":
            return f"${node.attrs.get('alttext', '').strip()}$"
        inner = "".join(self.inline(c, pre or tag == "pre") for c in node.children)
        if tag in ("b", "strong") and inner.strip():
            return f"**{inner.strip()}**"
        if tag in ("i", "em") and inner.strip():
            return f"*{inner.strip()}*"
        if tag == "code" and inner.strip():
            return f"`{inner.strip()}`"
        if tag == "abbr" and node.attrs.get("title"):
            return f"{inner}（{node.attrs['title']}）"
        if tag == "a":
            href = node.attrs.get("href", "")
            if "external" in node.cls() and href and inner.strip() and inner.strip() != href:
                return f"{inner.strip()}（{href}）"
            if "external" in node.cls() and href:
                return href
        return inner

    def skipped(self, node: Node) -> bool:
        if node.tag in ("script", "style", "noscript", "link", "meta"):
            return True
        if node.cls() & SKIP_CLASSES:
            return True
        style = node.attrs.get("style", "").replace(" ", "").lower()
        return "display:none" in style

    # 块级内容：返回若干段落，段落之间空一行
    def block(self, node: Node, depth: int = 0) -> str:
        parts: list[str] = []
        buffer: list[str] = []

        def flush():
            text = "".join(buffer)
            text = "\n".join(line.strip() for line in text.split("\n"))
            if text.strip():
                parts.append(text.strip())
            buffer.clear()

        for child in node.children:
            if isinstance(child, str):
                buffer.append(self.inline(child))
                continue
            if self.skipped(child):
                continue
            tag = child.tag
            if tag not in BLOCK_TAGS and not self.contains_block(child):
                buffer.append(self.inline(child))
                continue
            flush()
            rendered = self.render_block(child, depth)
            if rendered.strip():
                parts.append(rendered.strip("\n"))
        flush()
        return "\n\n".join(parts)

    def contains_block(self, node: Node) -> bool:
        for child in node.children:
            if isinstance(child, Node) and not self.skipped(child):
                if child.tag in BLOCK_TAGS or self.contains_block(child):
                    return True
        return False

    def render_block(self, node: Node, depth: int) -> str:
        tag = node.tag
        if re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            return "#" * level + " " + self.inline(node).strip()
        if tag == "hr":
            return "---"
        if tag == "pre":
            text = "".join(self.inline(c, pre=True) for c in node.children)
            return "```\n" + text.strip("\n") + "\n```"
        if tag in ("ul", "ol"):
            return self.render_list(node, depth)
        if tag == "dl":
            lines = []
            for child in node.children:
                if isinstance(child, Node) and child.tag == "dt":
                    lines.append(f"**{self.inline(child).strip()}**")
                elif isinstance(child, Node) and child.tag == "dd":
                    body = self.block(child, depth)
                    lines.append("\n".join("> " + line if line else ">" for line in body.split("\n")))
            return "\n\n".join(lines)
        if tag == "table":
            return self.render_table(node)
        if tag == "blockquote":
            body = self.block(node, depth)
            return "\n".join("> " + line if line else ">" for line in body.split("\n"))
        if tag == "article" and (node.attrs.get("data-mw-tab-title") or node.attrs.get("data-title")):
            title = node.attrs.get("data-mw-tab-title") or node.attrs.get("data-title")
            return f"**［标签页：{title}］**\n\n" + self.block(node, depth)
        return self.block(node, depth)

    def render_list(self, node: Node, depth: int) -> str:
        lines = []
        index = 0
        for child in node.children:
            if not isinstance(child, Node) or child.tag != "li" or self.skipped(child):
                continue
            index += 1
            marker = f"{index}." if node.tag == "ol" else "-"
            head: list[str] = []
            tail: list[str] = []
            for grandchild in child.children:
                if isinstance(grandchild, Node) and grandchild.tag in ("ul", "ol"):
                    tail.append(self.render_list(grandchild, depth + 1))
                elif isinstance(grandchild, Node) and not self.skipped(grandchild) and (
                        grandchild.tag in BLOCK_TAGS or self.contains_block(grandchild)):
                    text = self.render_block(grandchild, depth + 1).strip()
                    if text:
                        head.append(" " + text.replace("\n", "\n" + "  " * (depth + 1)))
                else:
                    head.append(self.inline(grandchild))
            text = re.sub(r"[ \t]+", " ", "".join(head)).strip()
            text = text.replace("\n", "\n" + "  " * (depth + 1))
            lines.append("  " * depth + f"{marker} {text}")
            lines.extend(tail)
        return "\n".join(lines)

    def render_table(self, node: Node) -> str:
        rows: list[Node] = []

        def collect(n: Node):
            for c in n.children:
                if isinstance(c, Node):
                    if c.tag == "tr":
                        rows.append(c)
                    elif c.tag in ("thead", "tbody", "tfoot"):
                        collect(c)
        collect(node)
        caption = next((c for c in node.children if isinstance(c, Node) and c.tag == "caption"), None)
        grid: dict[tuple[int, int], str] = {}
        width = 0
        for r, row in enumerate(rows):
            col = 0
            for cell in row.children:
                if not isinstance(cell, Node) or cell.tag not in ("td", "th"):
                    continue
                while (r, col) in grid:
                    col += 1
                text = self.block(cell).replace("\n\n", "<br>").replace("\n", "<br>").replace("|", "\\|").strip()
                if cell.tag == "th" and text:
                    text = f"**{text}**" if not text.startswith("**") else text
                try:
                    rowspan = max(1, int(cell.attrs.get("rowspan", "1") or 1))
                    colspan = max(1, int(cell.attrs.get("colspan", "1") or 1))
                except ValueError:
                    rowspan = colspan = 1
                for dr in range(rowspan):
                    for dc in range(colspan):
                        grid[(r + dr, col + dc)] = text if (dr, dc) == (0, 0) else ("〃" if text else "")
                col += colspan
                width = max(width, col)
        if not rows or width == 0:
            return ""
        out = []
        if caption is not None:
            out.append(f"**{self.inline(caption).strip()}**\n")
        nrows = max(r for r, _ in grid) + 1 if grid else 0
        for r in range(nrows):
            cells = [grid.get((r, c), "") for c in range(width)]
            out.append("| " + " | ".join(cells) + " |")
            if r == 0:
                out.append("|" + "---|" * width)
        return "\n".join(out)


def html_to_markdown(text: str) -> tuple[str, list[str]]:
    builder = TreeBuilder()
    builder.feed(text)
    builder.close()
    renderer = Renderer()
    body = renderer.block(builder.root)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n", renderer.media


def safe_name(title: str) -> str:
    return re.sub(r"[^\w\-]+", "_", title).strip("_") or "page"


def fetch_wiki(out_dir: Path) -> tuple[list[dict], str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rights = api({"action": "query", "meta": "siteinfo", "siprop": "rightsinfo"})["query"]["rightsinfo"]
    license_text = rights.get("text") or "见站点说明"
    pages = []
    for title in tech_pages():
        data = api({"action": "parse", "page": title, "prop": "text|revid|displaytitle", "redirects": "1",
                    "disableeditsection": "1", "disabletoc": "1"})["parse"]
        body, media = html_to_markdown(data["text"])
        url = WIKI_BASE + urllib.parse.quote(data["title"].replace(" ", "_"))
        name = safe_name(data["title"])
        header = (f"# {data['title']}\n\n"
                  f"> 来源：Celeste Wiki（celeste.ink）[{data['title']}]({url})，修订号 {data['revid']}，"
                  f"抓取于 {_dt.date.today().isoformat()}。许可：{license_text}。\n"
                  f"> 原文照录（由渲染后的页面转为 Markdown）；［图：…］［视频：…］是原页面上的图片与视频，这里只保留文件名。\n\n")
        (out_dir / f"{name}.md").write_text(header + body, encoding="utf-8")
        pages.append({"title": data["title"], "file": f"celeste_wiki/{name}.md", "revid": data["revid"],
                      "chars": len(body), "media": len(media)})
        print(f"  wiki：{data['title']}（{len(body)} 字符）", flush=True)
    return pages, license_text


# ---------------------------------------------------------------- Strawberry Jam

KEY_LINE = re.compile(r"^(\w+)=(.*)$")
INSERT = re.compile(r"\{\+\s*(.*?)\}")
COMMAND = re.compile(r"\{(.*?)\}")


def parse_dialog(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """按游戏自己的规则解析对话文件：'#' 开头为注释；'键=' 开始新条目；后续每一行是同一条目的下一页（{break}）。
    返回 (条目, 条目前最近的注释标题)。"""
    entries: dict[str, str] = {}
    comments: dict[str, str] = {}
    key = None
    last_comment = ""
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            last_comment = line.lstrip("#").strip()
            continue
        line = line.replace("\\#", "#")
        match = KEY_LINE.match(line)
        if match:
            if key is not None:
                entries[key] = "{break}".join(buf)
            key = match.group(1)
            buf = [match.group(2).strip()] if match.group(2).strip() else []
            comments[key] = last_comment
        elif key is not None:
            buf.append(line)
    if key is not None:
        entries[key] = "{break}".join(buf)
    return entries, comments


def clean_dialog(value: str, entries: dict[str, str]) -> str:
    """展开 {+键} 插入，{n} 换行、{break} 分页（这里写成分段），其余显示用的命令（颜色、字号、停顿、动画）去掉。"""
    for _ in range(5):
        new = INSERT.sub(lambda m: entries.get(m.group(1), ""), value)
        if new == value:
            break
        value = new
    value = value.replace("{n}", "\n").replace("{break}", "\n\n")
    value = COMMAND.sub("", value)
    value = re.sub(r"\\(.)", r"\1", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def topic_of(key: str, prefix: str) -> str:
    return re.sub(r"\d+$", "", key[len(prefix):])


def write_sj(out_dir: Path, lang: str, entries: dict[str, str], comments: dict[str, str], source: str) -> list[dict]:
    files = []
    today = _dt.date.today().isoformat()
    note = (f"> 来源：本机安装的 Strawberry Jam Collab（草莓酱）Mod，`{SJ_ZIP}` 内的 `{source}`，抓取于 {today}。\n"
            f"> 原文照录；游戏里每一页对话框在这里是一段，去掉了颜色、字号、停顿等显示用的格式代码。\n"
            f"> 小标题是对话条目的键名（同一键名在英文版与中文版里对应同一段文字）。\n\n")

    def section(keys: list[str], prefix: str) -> list[str]:
        lines = []
        current = None
        for key in keys:
            group = comments.get(key) or topic_of(key, prefix)
            if group != current:
                current = group
                lines.append(f"## {group}\n")
            lines.append(f"### {key[len('SJ2021_'):]}\n")
            lines.append(clean_dialog(entries[key], entries) + "\n")
        return lines

    # 训练场：按难度分组
    gym_lines = [f"# 草莓酱训练场（Beginner → Grandmaster）说明文字（{'English' if lang == 'en' else '简体中文'}）\n", note]
    for level in SJ_LEVELS:
        prefix = f"SJ2021_Gym_{level}_"
        keys = [k for k in entries if k.startswith(prefix)]
        name_key = next((k for k in entries if re.fullmatch(rf"StrawberryJam2021_0_Gyms_\d+_{level}", k)), None)
        title = clean_dialog(entries[name_key], entries) if name_key else level
        gym_lines.append(f"\n# {level}（{title}）\n")
        gym_lines.extend(section(keys, prefix))
    others = [k for k in entries if k.startswith("SJ2021_Gym_")
              and not any(k.startswith(f"SJ2021_Gym_{lv}_") for lv in SJ_LEVELS)]
    others += [k for k in entries if k.startswith("SJ2021_lobby_gym_")]
    gym_lines.append("\n# 附：训练场里的其他界面文字（房间名、速度对照表等）\n")
    gym_lines.append("| 键名 | 文字 |\n|---|---|")
    for key in others:
        gym_lines.append(f"| {key} | {clean_dialog(entries[key], entries).replace(chr(10), '<br>').replace('|', '¦')} |")
    path = out_dir / f"gyms_{lang}.md"
    path.write_text("\n".join(gym_lines).strip() + "\n", encoding="utf-8")
    files.append({"file": f"strawberry_jam/{path.name}", "keys": sum(1 for k in entries if k.startswith("SJ2021_Gym_"))})

    lib_keys = [k for k in entries if k.startswith("SJ2021_Library_")]
    lib_lines = [f"# 草莓酱机制库（Library）全文（{'English' if lang == 'en' else '简体中文'}）\n", note]
    lib_lines.extend(section(lib_keys, "SJ2021_Library_"))
    path = out_dir / f"library_{lang}.md"
    path.write_text("\n".join(lib_lines).strip() + "\n", encoding="utf-8")
    files.append({"file": f"strawberry_jam/{path.name}", "keys": len(lib_keys)})
    return files


def fetch_sj(celeste_dir: Path, out_dir: Path) -> list[dict]:
    zip_path = celeste_dir / "Mods" / SJ_ZIP
    if not zip_path.exists():
        raise SystemExit(f"找不到 {zip_path}（需要先安装 Strawberry Jam Collab）")
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with zipfile.ZipFile(zip_path) as archive:
        for lang, member in SJ_LANGS.items():
            text = archive.read(member).decode("utf-8-sig")
            entries, comments = parse_dialog(text)
            written = write_sj(out_dir, lang, entries, comments, member)
            for item in written:
                print(f"  草莓酱：{item['file']}（{item['keys']} 条）", flush=True)
            files.extend(written)
    return files


# ---------------------------------------------------------------- 索引

def write_index(out: Path, wiki: list[dict], license_text: str, sj: list[dict]) -> None:
    lines = [
        "# 参考资料原文（reference/）",
        "",
        "这里是两份外部资料的原文照录，由 `tools/fetch_reference.py` 在本机生成，不提交到仓库。",
        "它们是社区资料，写法、数值精度与本知识库其他文件不同：**与 `techniques.md`、`player_physics.md` 等"
        "（已对照游戏代码核实）冲突时，以那些文件为准。** 原文里偶尔提到的具体章节、房间、速通路线只是举例背景，"
        "本轮挑战的路线仍要你自己根据房间数据规划。",
        "",
        f"## Celeste Wiki（celeste.ink）技巧页面（许可：{license_text}）",
        "",
        "| 页面 | 文件 | 字符数 |",
        "|---|---|---|",
    ]
    for page in wiki:
        lines.append(f"| {page['title']} | `{page['file']}` | {page['chars']} |")
    lines += [
        "",
        "`Tech.md` 是总表（机制、冲刺技巧、无冲刺技巧、实体技巧、其他技巧），其余是总表里各大类的详细子页面。",
        "",
        "## 草莓酱（Strawberry Jam Collab）训练场与机制库",
        "",
        "| 文件 | 内容 | 条目数 |",
        "|---|---|---|",
    ]
    desc = {"gyms": "训练场 Beginner → Grandmaster 各房间说明", "library": "机制库全文"}
    for item in sj:
        kind = "gyms" if "gyms_" in item["file"] else "library"
        lang = "英文" if item["file"].endswith("_en.md") else "简体中文"
        lines.append(f"| `{item['file']}` | {desc[kind]}（{lang}） | {item['keys']} |")
    lines += ["", "训练场与机制库讲的技巧和机制都是原版游戏就有的。个别演示房间用 Mod 实体来摆位置或做演示"
              "（例如侧向的单向平台、改造过的梦境块），原版章节里没有这些实体，但讲的机制本身是原版的。", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--celeste-dir", default=r"D:\Fluency\software\steam\steamapps\common\Celeste",
                    help="蔚蓝安装目录（草莓酱 Mod 在其 Mods 子目录下）")
    ap.add_argument("--out", default=str(REPO_ROOT / "kb" / "reference"), help="输出目录")
    ap.add_argument("--skip-wiki", action="store_true")
    ap.add_argument("--skip-sj", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wiki: list[dict] = []
    license_text = ""
    sj: list[dict] = []
    if not args.skip_wiki:
        print("抓取 Celeste Wiki 技巧页面…", flush=True)
        wiki, license_text = fetch_wiki(out / "celeste_wiki")
    if not args.skip_sj:
        print("读取草莓酱训练场与机制库文本…", flush=True)
        sj = fetch_sj(Path(args.celeste_dir), out / "strawberry_jam")
    write_index(out, wiki, license_text, sj)
    print(f"完成：{out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
