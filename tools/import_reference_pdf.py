"""把一份 PDF 资料导入知识库的社区资料目录（只放本机，不提交到仓库）。

文字版：按页提取 PDF 的文字层，每页前标 `<!-- 原文第 N 页 -->`，写成 Markdown；原 PDF 复制一份，
智能体需要看插图时可以用 Read 打开它（pages 参数指定页码）。导入后重新生成 kb/reference/README.md。

用法（仓库根目录）：
    python tools/import_reference_pdf.py 资料.pdf --name celeste_mechanics_explained --title "Celeste 机制详解（反向钟）" \
        --note "来源：……"

提取文字优先用 poppler 的 pdftotext（Windows 上需自行安装并加入 PATH），没有时用 pypdf（pip install pypdf）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from fetch_reference import write_index  # noqa: E402


def extract_pages(pdf: Path) -> list[str]:
    if shutil.which("pdftotext"):
        raw = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, encoding="utf-8").stdout
        pages = raw.split("\f")
        return pages[:-1] if pages and not pages[-1].strip() else pages
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("需要 pdftotext（poppler）或 pypdf：pip install pypdf") from exc
    return [page.extract_text() or "" for page in PdfReader(str(pdf)).pages]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--name", required=True, help="文件名（不含扩展名），如 celeste_mechanics_explained")
    ap.add_argument("--title", required=True, help="文档标题")
    ap.add_argument("--note", default="", help="写在开头的来源说明")
    ap.add_argument("--out", default=str(REPO_ROOT / "kb" / "reference"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    pdf = Path(args.pdf)
    out = Path(args.out)
    community = out / "community"
    (community / "pdf").mkdir(parents=True, exist_ok=True)
    pages = extract_pages(pdf)
    note = (f"> {args.note}\n" if args.note else "") + (
        f"> 原文照录（由 PDF 文字层提取），项目作者认为内容默认正确。插图在文字版里只剩图注；需要看图时用 Read 打开 "
        f"`/kb/reference/community/pdf/{args.name}.pdf`，用 pages 参数指定页码（文中以 `<!-- 原文第 N 页 -->` 标出）。")
    parts = [f"# {args.title}\n", note, ""]
    for i, page in enumerate(pages, 1):
        parts.append(f"\n<!-- 原文第 {i} 页 -->\n")
        parts.append(re.sub(r"\n{3,}", "\n\n", page.strip()))
    (community / f"{args.name}.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
    shutil.copy(pdf, community / "pdf" / f"{args.name}.pdf")
    write_index(out)
    print(f"已导入 {len(pages)} 页：{community / (args.name + '.md')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
