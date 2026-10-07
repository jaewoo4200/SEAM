#!/usr/bin/env python
"""Build the website guide pages from the Markdown guides.

Reads ``docs/guides/<name>.md`` (English) and ``docs/guides/<name>.ko.md``
(Korean) and writes ``website/en/guides/<name>.html`` and
``website/ko/guides/<name>.html`` for the eight user guides. The page chrome
(head, styles, top bar, footer, theme script) is taken from the committed
``website/<lang>/guides/simulation.html``; the side navigation, eyebrow, title,
description, canonical/hreflang links and previous/next links are rebuilt for
every page. ``website/<lang>/guides/index.html`` is hand-written and untouched.

No third-party dependency: a small Markdown converter covers what the guides
use (headings, paragraphs, nested lists, tables, fenced and inline code,
bold/italic, links, images with an italic caption line, blockquotes, rules).

Conversion rules:
- the language line under the H1 (``> **English** · [한국어](x.ko.md)``) is dropped;
- the first paragraph after it becomes ``<p class="lead">``, the first ``---``
  the colored ``<div class="spectrum">`` (later rules are dropped: h2 already
  draws a divider);
- ``![alt](../images/NN_x.png)`` (+ an optional ``*caption*`` line right under
  it) becomes a ``figure.doc-shot`` pointing at ``../../assets/guides/NN_x.png``;
  a screenshot missing from ``website/assets/guides/`` is copied there;
- links to sibling guides (``x.md`` / ``x.ko.md``) become ``x.html``, other
  repo links point at the file on GitHub (master), external links are kept;
- headings get GitHub-style ids, so ``#section`` links keep working.

After writing, every local link and ``#anchor`` across the generated pages is
checked and problems are reported (exit code 1 with --strict).

Usage (from the repo root)::

    python scripts/build_website_guides.py              # writes website/{en,ko}/guides/
    python scripts/build_website_guides.py --out DIR    # dry run: writes DIR/{en,ko}/guides/
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_GUIDES = REPO_ROOT / "docs" / "guides"
DOCS_IMAGES = REPO_ROOT / "docs" / "images"
WEBSITE = REPO_ROOT / "website"
GITHUB_BLOB = "https://github.com/jaewoo4200/SEAM/blob/master/"
TEMPLATE_NAME = "simulation"
LANGS = ("en", "ko")

# (name, side-navigation label per language), in guide order (eyebrow "Guide NN").
GUIDES: list[tuple[str, dict[str, str]]] = [
    ("getting_started", {"en": "Getting started", "ko": "시작하기"}),
    ("scene_import", {"en": "Importing scenes", "ko": "씬 임포트"}),
    ("materials_and_ai", {"en": "RF materials & AI", "ko": "RF 재질 & AI"}),
    ("simulation", {"en": "Simulating", "ko": "시뮬레이션"}),
    ("trajectory_uav", {"en": "Trajectories & UAVs", "ko": "궤적 & UAV"}),
    ("datasets_export", {"en": "ML datasets & exports", "ko": "ML 데이터셋 & 내보내기"}),
    ("sensing", {"en": "Radar sensing & ISAC", "ko": "레이더 센싱 & ISAC"}),
    ("playback_dashboard", {"en": "Multimodal playback", "ko": "멀티모달 재생"}),
]
GUIDE_NAMES = [name for name, _ in GUIDES]

DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("getting_started", "en"): "From first launch to a full tour of the SEAM Studio window — the toolbar, the five mode tabs, the scene tree, the inspector, and the dockable panels — all on the built-in Mock backend.",
    ("getting_started", "ko"): "설치 직후 첫 실행부터 툴바, 다섯 가지 모드 탭, 씬 트리, 인스펙터, 도킹 패널까지 내장 Mock 백엔드만으로 SEAM Studio 화면을 한 바퀴 둘러보는 가이드입니다.",
    ("scene_import", "en"): "How to import Mitsuba/Sionna XML scenes, build outdoor scenes from OpenStreetMap areas, and load devices and UE routes from JSON in SEAM Studio.",
    ("scene_import", "ko"): "SEAM Studio에서 Mitsuba/Sionna XML 씬을 불러오고, OpenStreetMap 영역으로 실외 씬을 만들고, JSON으로 디바이스와 UE 경로를 추가하는 방법을 안내합니다.",
    ("materials_and_ai", "en"): "How to assign RF materials from the library, validate scene readiness, and use AI-assisted suggestions to turn visual geometry into a simulatable digital twin in SEAM Studio.",
    ("materials_and_ai", "ko"): "SEAM Studio에서 RF 재질 라이브러리로 재질을 지정하고, 검증으로 누락을 찾고, AI 지원 제안으로 시각적 지오메트리를 시뮬레이션 가능한 디지털 트윈으로 만드는 방법을 안내합니다.",
    ("simulation", "en"): "Run ray-path solves, coverage radio maps, MIMO beamforming, and per-link channel analysis in SEAM Studio's Results mode — with or without Sionna RT.",
    ("simulation", "ko"): "SEAM Studio의 Results 모드에서 레이 경로, 커버리지 라디오맵, MIMO 빔포밍, 링크별 채널 분석을 실행하고 결과를 읽는 방법을 안내합니다.",
    ("trajectory_uav", "en"): "How to sweep a UE along a route or fly a UAV with an attached antenna in SEAM Studio, play back per-step rays and KPIs, and use the per-entity POV inset.",
    ("trajectory_uav", "ko"): "SEAM Studio에서 UE를 경로로 움직이거나 안테나를 부착한 UAV를 비행시키고, 스텝별 레이 재생과 엔티티별 POV 인셋을 활용하는 방법을 설명합니다.",
    ("datasets_export", "en"): "Export ML-ready NumPy .npz ground-truth datasets, AODT-compatible RFData bundles, WYSIWYG viewport captures, and per-chart CSV/PNG/SVG figures from SEAM Studio.",
    ("datasets_export", "ko"): "SEAM Studio에서 ML 학습용 NumPy .npz 그라운드트루스 데이터셋, AODT 호환 RFData 번들, WYSIWYG 뷰포트 캡처, 차트별 CSV/PNG/SVG 내보내기를 다루는 가이드.",
    ("sensing", "en"): "Radar sensing in SEAM Studio: RCS targets, echo solves with Doppler, ISAC over time with EKF tracking, beam trade-offs, sensing coverage maps, Pd/Pfa detectors, and labeled sensing datasets.",
    ("sensing", "ko"): "SEAM Studio의 레이더 센싱 가이드: RCS 타깃, 도플러를 포함한 에코 솔브, EKF 추적이 들어간 시간축 ISAC, 빔 트레이드오프, 센싱 커버리지 맵, Pd/Pfa 검출기, 라벨이 붙은 센싱 데이터셋.",
    ("playback_dashboard", "en"): "Replay a recorded drive or flight (camera, LiDAR, measured beam power, UE pose) frame by frame against the SEAM Studio digital twin, with beam curves, KPIs and the measured beam lobe side by side.",
    ("playback_dashboard", "ko"): "실제 주행·비행 기록(카메라, LiDAR, 실측 빔 파워, UE 자세)을 SEAM Studio 디지털 트윈과 프레임 단위로 나란히 재생하고, 빔 곡선·KPI·실측 빔 로브로 비교하는 방법을 안내합니다.",
}

UI = {
    "en": {"cap": "User guides", "all": "← All guides", "prev": "Previous", "next": "Next"},
    "ko": {"cap": "사용 가이드", "all": "← 전체 가이드", "prev": "이전", "next": "다음"},
}

IND = "    "  # article children indent (matches the committed pages)


# --------------------------------------------------------------------- inline


@dataclass
class Page:
    name: str
    lang: str
    md_path: Path
    images: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    slugs: dict[str, int] = field(default_factory=dict)


_ESCAPABLE = r"""!"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~"""
_CODE_RE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)", re.S)
_ESC_RE = re.compile(r"\\([" + _ESCAPABLE + r"])")
_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)')
_LINK_RE = re.compile(r'\[((?:[^\[\]]|\[[^\]]*\])+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)')
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", re.S)
# '*' may emphasize inside a word (Korean particles follow it: *강조*를), '_' may not.
_EM_STAR_RE = re.compile(r"(?<!\*)\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?!\*)", re.S)
_EM_UND_RE = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", re.S)
_PH_RE = re.compile("\x00(\\d+)\x00")


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def rewrite_href(url: str, page: Page) -> str:
    """Map a Markdown link target to its website URL."""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) or url.startswith(("//", "#")):
        return url
    path, sep, frag = url.partition("#")
    frag = sep + frag
    if not path:
        return url
    target = (page.md_path.parent / path).resolve()
    try:
        rel = target.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        page.warnings.append(f"link outside the repo kept as is: {url}")
        return url
    if target.parent == DOCS_GUIDES.resolve():
        stem = target.name.removesuffix(".md").removesuffix(".ko")
        if target.suffix == ".md" and stem in GUIDE_NAMES:
            return f"{stem}.html{frag}"
    if not target.exists():
        page.warnings.append(f"link to a missing repo path: {url}")
    return f"{GITHUB_BLOB}{rel}{frag}"


def image_src(src: str, page: Page) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src):
        return src
    target = (page.md_path.parent / src).resolve()
    if target.parent == DOCS_IMAGES.resolve():
        page.images.append(target.name)
        return f"../../assets/guides/{target.name}"
    return rewrite_href(src, page) + "?raw=true"


def inline(text: str, page: Page) -> str:
    """Render one block's inline Markdown to HTML (text may span lines)."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def code(m: re.Match[str]) -> str:
        body = m.group(2).replace("\n", " ")
        if body.startswith(" ") and body.endswith(" ") and body.strip():
            body = body[1:-1]
        return keep(f"<code>{html.escape(body, quote=False)}</code>")

    text = _CODE_RE.sub(code, text)
    text = _ESC_RE.sub(lambda m: keep(html.escape(m.group(1), quote=False)), text)
    text = html.escape(text, quote=False)

    def img(m: re.Match[str]) -> str:
        alt = html.unescape(m.group(1))
        src = image_src(html.unescape(m.group(2)), page)
        return keep(f'<img loading="lazy" decoding="async" src="{_attr(src)}" alt="{_attr(alt)}">')

    def link(m: re.Match[str]) -> str:
        href = rewrite_href(html.unescape(m.group(2)), page)
        title = f' title="{_attr(html.unescape(m.group(3)))}"' if m.group(3) else ""
        return keep(f'<a href="{_attr(href)}"{title}>') + m.group(1) + keep("</a>")

    text = _IMG_RE.sub(img, text)
    text = _LINK_RE.sub(link, text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _EM_STAR_RE.sub(r"<em>\1</em>", text)
    text = _EM_UND_RE.sub(r"<em>\1</em>", text)
    while _PH_RE.search(text):
        text = _PH_RE.sub(lambda m: stash[int(m.group(1))], text)
    return text


def plain_text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def github_slug(text: str, page: Page) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation/symbols, spaces -> '-'."""
    kept = "".join(
        ch for ch in text.lower() if ch in " -_" or unicodedata.category(ch)[0] in "LMN"
    )
    slug = kept.replace(" ", "-")
    n = page.slugs.get(slug, 0)
    page.slugs[slug] = n + 1
    return slug if n == 0 else f"{slug}-{n}"


# ---------------------------------------------------------------------- blocks

_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?:( +)(.*))?$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)$")
_CAPTION_RE = re.compile(r"^(\*|_)(?=\S)(.+)(?<=\S)\1$", re.S)

HR = "\x01hr\x01"


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        lines[i].lstrip().startswith("|")
        and i + 1 < len(lines)
        and bool(_TABLE_SEP_RE.match(lines[i + 1]))
    )


def _interrupts_paragraph(lines: list[str], i: int) -> bool:
    line = lines[i]
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line):
        return True
    if line.lstrip().startswith(">") or _is_table_start(lines, i):
        return True
    m = _LIST_RE.match(line)
    # As CommonMark: a bullet, or an ordered item numbered 1, may interrupt.
    return bool(m and m.group(4) and (not m.group(2)[0].isdigit() or m.group(2)[:-1] == "1"))


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _align(sep_cell: str) -> str:
    s = sep_cell.strip()
    if s.startswith(":") and s.endswith(":"):
        return ' style="text-align:center"'
    if s.endswith(":"):
        return ' style="text-align:right"'
    return ""


def render_table(lines: list[str], page: Page, ind: str) -> str:
    head = _split_row(lines[0])
    aligns = [_align(c) for c in _split_row(lines[1])]
    aligns += [""] * (len(head) - len(aligns))
    out = [f"{ind}<table>", f"{ind}  <thead>"]
    out.append(f"{ind}    <tr>" + "".join(f"<th{aligns[k]}>{inline(c, page)}</th>" for k, c in enumerate(head)) + "</tr>")
    out += [f"{ind}  </thead>", f"{ind}  <tbody>"]
    for row in lines[2:]:
        cells = _split_row(row)
        cells = (cells + [""] * len(head))[: len(head)]
        out.append(f"{ind}    <tr>" + "".join(f"<td{aligns[k]}>{inline(c, page)}</td>" for k, c in enumerate(cells)) + "</tr>")
    out += [f"{ind}  </tbody>", f"{ind}</table>"]
    return "\n".join(out)


def render_paragraph(para: list[str], page: Page, ind: str) -> str:
    m = _IMAGE_LINE_RE.match(para[0].strip())
    if m:
        alt, src = m.group(1), m.group(2)
        rest = "\n".join(para[1:]).strip()
        cap = _CAPTION_RE.match(rest) if rest else None
        caption = inline(cap.group(2), page) if cap else html.escape(alt, quote=False)
        figure = (
            f'{ind}<figure class="doc-shot"><img loading="lazy" decoding="async" '
            f'src="{_attr(image_src(src, page))}" alt="{_attr(alt)}"><figcaption>{caption}</figcaption></figure>'
        )
        if rest and not cap:
            return figure + "\n\n" + render_paragraph(para[1:], page, ind)
        return figure
    body = inline("\n".join(para), page).replace("\n", "\n" + ind)
    return f"{ind}<p>{body}</p>"


def parse_list(lines: list[str], i: int, page: Page, ind: str) -> tuple[str, int]:
    first = _LIST_RE.match(lines[i])
    assert first
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items: list[list[str]] = []
    loose = False
    n = len(lines)
    while i < n:
        m = _LIST_RE.match(lines[i])
        if not m or len(m.group(1)) != base or m.group(2)[0].isdigit() != ordered:
            break
        spaces = len(m.group(3) or " ")
        content_indent = base + len(m.group(2)) + (spaces if spaces <= 4 else 1)
        body = [m.group(4) or ""]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _indent_of(lines[j]) >= content_indent:
                    body += [""] * (j - i)
                    i = j
                    continue
                if j < n and j > i:
                    nxt = _LIST_RE.match(lines[j])
                    sibling = nxt and len(nxt.group(1)) == base and nxt.group(2)[0].isdigit() == ordered
                    # A blank line that only closes a table/list/code block inside
                    # the item is not the author spacing the list out.
                    simple = all(b.strip() and not _LIST_RE.match(b) and not b.lstrip().startswith(("|", "```", "~~~")) for b in body)
                    if sibling and simple:
                        loose = True
                i = j
                break
            ind_now = _indent_of(line)
            item = _LIST_RE.match(line)
            is_item = bool(item and item.group(4) is not None)
            if ind_now >= content_indent:
                body.append(line[content_indent:])
            elif is_item and ind_now <= base:
                break  # next sibling (or an outer list's item)
            elif is_item:
                body.append(line[ind_now:])  # nested item indented less than the content
            elif body[-1].strip() and not _interrupts_paragraph(lines, i):
                body.append(line.strip())  # lazy continuation
            else:
                break
            i += 1
        while body and not body[-1].strip():
            body.pop()
        items.append(body)
        if i < n and not lines[i].strip():
            break
    tag = "ol" if ordered else "ul"
    attrs = f' start="{start}"' if ordered and start != 1 else ""
    out = [f"{ind}<{tag}{attrs}>"]
    p_open = f"{ind}    <p>"
    for body in items:
        blocks = [b for b in parse_blocks(body, page, ind + "    ") if b != HR]
        if not loose and blocks and blocks[0].startswith(p_open) and blocks[0].endswith("</p>"):
            # tight list (no blank line between items): first paragraph as bare text
            first = blocks[0][len(p_open):-len("</p>")]
            rest = "".join("\n" + b for b in blocks[1:])
            out.append(f"{ind}  <li>{first}{rest}" + (f"\n{ind}  </li>" if rest else "</li>"))
        else:
            out.append(f"{ind}  <li>\n" + "\n".join(blocks) + f"\n{ind}  </li>")
    out.append(f"{ind}</{tag}>")
    return "\n".join(out), i


def parse_blocks(lines: list[str], page: Page, ind: str = IND) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            pad, marker = len(fence.group(1)), fence.group(2)
            body: list[str] = []
            i += 1
            while i < n:
                s = lines[i].strip()
                if s.startswith(marker[0] * len(marker)) and not s.strip(marker[0]):
                    i += 1
                    break
                body.append(lines[i][pad:] if lines[i][:pad].strip() == "" else lines[i].lstrip())
                i += 1
            out.append(f"{ind}<pre><code>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            content = inline(heading.group(2), page)
            slug = github_slug(plain_text(content), page)
            out.append(f'{ind}<h{level} id="{_attr(slug)}">{content}</h{level}>')
            i += 1
            continue
        if _HR_RE.match(line):
            out.append(HR)
            i += 1
            continue
        if _is_table_start(lines, i):
            rows = [lines[i], lines[i + 1]]
            i += 2
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(render_table(rows, page, ind))
            continue
        if line.lstrip().startswith(">"):
            quoted: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                s = lines[i].lstrip()[1:]
                quoted.append(s[1:] if s.startswith(" ") else s)
                i += 1
            inner = parse_blocks(quoted, page, ind + "  ")
            out.append(f"{ind}<blockquote>\n" + "\n".join(b for b in inner if b != HR) + f"\n{ind}</blockquote>")
            continue
        m = _LIST_RE.match(line)
        if m and m.group(4) is not None:
            rendered, i = parse_list(lines, i, page, ind)
            out.append(rendered)
            continue
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _interrupts_paragraph(lines, i):
            para.append(lines[i].strip())
            i += 1
        if len(para) == 1 and _IMAGE_LINE_RE.match(para[0]):
            # an italic caption paragraph right under a screenshot belongs to it
            j = i
            while j < n and not lines[j].strip():
                j += 1
            k = j
            while k < n and lines[k].strip() and not _interrupts_paragraph(lines, k):
                k += 1
            if j > i and k > j and _CAPTION_RE.match("\n".join(x.strip() for x in lines[j:k])):
                para += [x.strip() for x in lines[j:k]]
                i = k
        out.append(render_paragraph(para, page, ind))
    return out


# ------------------------------------------------------------------------ page


@dataclass
class Guide:
    name: str
    lang: str
    title_html: str
    title_text: str
    body: str
    page: Page


def convert(name: str, lang: str) -> Guide:
    md_path = DOCS_GUIDES / (f"{name}.md" if lang == "en" else f"{name}.ko.md")
    page = Page(name=name, lang=lang, md_path=md_path)
    lines = md_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    if i == len(lines):
        raise SystemExit(f"{md_path}: no '# ' title line")
    title_html = inline(lines[i][2:].strip(), page)
    github_slug(plain_text(title_html), page)  # GitHub gives the H1 an anchor too
    i += 1
    # language switch line ("> **English** · [한국어](x.ko.md)")
    j = i
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and lines[j].lstrip().startswith(">") and "English" in lines[j] and "한국어" in lines[j]:
        i = j + 1
    blocks = parse_blocks(lines[i:], page)

    parts: list[str] = []
    lead_done = spectrum_done = False
    for block in blocks:
        if block == HR:
            if lead_done and not spectrum_done:
                parts.append(f'{IND}<div class="spectrum"></div>')
                spectrum_done = True
            continue
        if not lead_done:
            if block.startswith(f"{IND}<p>"):
                block = block.replace(f"{IND}<p>", f'{IND}<p class="lead">', 1).replace("\n" + IND, " ")
            lead_done = True
        elif not spectrum_done:
            parts.append(f'{IND}<div class="spectrum"></div>')
            spectrum_done = True
        parts.append(block)
    return Guide(name, lang, title_html, plain_text(title_html), "\n\n".join(parts), page)


def load_template(lang: str) -> tuple[str, str]:
    """Split the committed template page into (head + top bar, footer + script)."""
    path = WEBSITE / lang / "guides" / f"{TEMPLATE_NAME}.html"
    text = path.read_text(encoding="utf-8")
    start = text.find('<div class="wrap docwrap">')
    end = text.find("\n<footer>")
    if start < 0 or end < 0:
        raise SystemExit(f"{path}: template markers not found (docwrap / footer)")
    head, foot = text[:start], text[end:]
    for needle in ("<title>", '<meta name="description"', 'rel="canonical"', f"guides/{TEMPLATE_NAME}.html"):
        if needle not in head:
            raise SystemExit(f"{path}: template head lacks {needle!r}")
    return head, foot


def render_page(guide: Guide, guides: dict[str, Guide], template: tuple[str, str]) -> str:
    head, foot = template
    lang, ui = guide.lang, UI[guide.lang]
    head = re.sub(r"<title>.*?</title>", lambda _: f"<title>{html.escape(guide.title_text, quote=False)} — SEAM Studio</title>", head, count=1, flags=re.S)
    head = re.sub(
        r'<meta name="description" content="[^"]*">',
        lambda _: f'<meta name="description" content="{_attr(DESCRIPTIONS[(guide.name, lang)])}">',
        head,
        count=1,
    )
    head = head.replace(f"guides/{TEMPLATE_NAME}.html", f"guides/{guide.name}.html")
    foot = foot.replace(f"guides/{TEMPLATE_NAME}.html", f"guides/{guide.name}.html")

    nav = ['  <aside class="sidenav">', f'    <p class="cap">{html.escape(ui["cap"], quote=False)}</p>']
    for name, labels in GUIDES:
        active = ' class="active"' if name == guide.name else ""
        nav.append(f'    <a{active} href="{name}.html">{html.escape(labels[lang], quote=False)}</a>')
    nav += [f'    <a class="all" href="./">{html.escape(ui["all"], quote=False)}</a>', "  </aside>"]

    idx = GUIDE_NAMES.index(guide.name)
    prev_g = guides[GUIDE_NAMES[idx - 1]] if idx > 0 else None
    next_g = guides[GUIDE_NAMES[idx + 1]] if idx + 1 < len(GUIDE_NAMES) else None
    pn = [f'{IND}<div class="pn">']
    pn.append(
        f'{IND}  <a href="{prev_g.name}.html"><span class="lbl">{ui["prev"]}</span>{prev_g.title_html}</a>'
        if prev_g
        else f"{IND}  <span></span>"
    )
    pn.append(
        f'{IND}  <a href="{next_g.name}.html"><span class="lbl">{ui["next"]}</span>{next_g.title_html}</a>'
        if next_g
        else f"{IND}  <span></span>"
    )
    pn.append(f"{IND}</div>")

    article = [
        "  <article>",
        f'{IND}<div class="eyebrow">Guide {idx + 1:02d} · SEAM Studio docs</div>',
        f"{IND}<h1>{guide.title_html}</h1>",
        guide.body,
        "",
        "\n".join(pn),
        "  </article>",
    ]
    return head + '<div class="wrap docwrap">\n' + "\n".join(nav) + "\n\n" + "\n".join(article) + "\n</div>\n" + foot


# ----------------------------------------------------------------------- check


def check_links(pages: dict[tuple[str, str], str]) -> list[str]:
    """Local hrefs and #anchors across the generated pages (same language)."""
    problems: list[str] = []
    ids = {key: set(re.findall(r'\sid="([^"]+)"', text)) for key, text in pages.items()}
    for (name, lang), text in pages.items():
        for href in re.findall(r'href="([^"]+)"', text):
            href = html.unescape(href)
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) or href.startswith(("./", "../")):
                continue
            path, _, frag = href.partition("#")
            target = name if not path else path.removesuffix(".html")
            if path and (not path.endswith(".html") or target not in GUIDE_NAMES):
                problems.append(f"{lang}/{name}.html: unexpected local link {href}")
                continue
            if frag and frag not in ids[(target, lang)]:
                problems.append(f"{lang}/{name}.html: anchor #{frag} not found in {target}.html")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None, help="output root (default: website/); writes <out>/{en,ko}/guides/")
    parser.add_argument("--strict", action="store_true", help="exit 1 when a link/anchor/image problem is found")
    args = parser.parse_args()
    out_root = (args.out or WEBSITE).resolve()

    problems: list[str] = []
    rendered: dict[tuple[str, str], str] = {}
    images: set[str] = set()
    for lang in LANGS:
        template = load_template(lang)
        guides = {name: convert(name, lang) for name in GUIDE_NAMES}
        for name, guide in guides.items():
            rendered[(name, lang)] = render_page(guide, guides, template)
            images.update(guide.page.images)
            problems += [f"{lang}/{name}.html: {w}" for w in guide.page.warnings]
    problems += check_links(rendered)

    for (name, lang), text in rendered.items():
        dest = out_root / lang / "guides" / f"{name}.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(rendered)} pages to {out_root / '{en,ko}' / 'guides'}")

    for image in sorted(images):
        src = DOCS_IMAGES / image
        if (WEBSITE / "assets" / "guides" / image).exists():
            continue
        if not src.exists():
            problems.append(f"image missing from docs/images: {image}")
            continue
        dest = out_root / "assets" / "guides" / image
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"copied screenshot {image} -> {dest}")

    for p in problems:
        print(f"warning: {p}", file=sys.stderr)
    print(f"{len(problems)} warning(s)")
    return 1 if (args.strict and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
