"""HTML quiz file → the bot's normal quiz import (Feature 1).

A quiz HTML file is parsed with the standard library (no extra dependency):
the DOM is rebuilt into a small tree, question containers / options / the
marked correct answer / explanations are recognised from explicit HTML
structure, and the result is handed to the **existing** import pipeline as a
``pdf_parser.ParseResult`` — the same preview → confirm → create → play flow.

What is recognised (all of it *explicit* in the HTML, nothing guessed):

* question containers: ``class``/``id`` such as ``question`` / ``ques1`` /
  ``mcq`` / ``item`` / ``प्रश्न``; otherwise groups of radio/checkbox inputs
  (<input name="q1"> …) whose lowest common parent is the question block;
  otherwise table rows with question + options + answer cells
* options: ``<input type=radio|checkbox>`` with ``<label>``, or elements whose
  class is ``option`` / ``choice`` / ``answer`` (when they are not an
  "Answer: B" line), or ``<li>`` items of a list inside the block
* the correct answer: ``checked``, ``class``/``id``/``data-*``/``aria-*``
  saying correct/right/true, an option starting with ``✓``, or an
  "Answer: B" / "उत्तर: (B)" / "सही उत्तर: C" line
* explanation: ``class="explanation" / "solution" / "व्याख्या"`` blocks or an
  "Explanation:" / "व्याख्या:" line

Green colour or bold-only marking is a **weak** signal: the answer is used but
the question is marked *Verification Required*, so the admin confirms it in the
existing preview step.  A question whose answer cannot be found is reported as
``Answer Not Found`` and **never** guessed.  Multi-answer HTML (2+ checked
options) is reported, because the bot's quiz polls are single-answer.

If no question containers are recognised, the file text is extracted exactly
like a .txt upload and the normal PDF/Text parser handles it — the report says
so and nothing is silently dropped.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable, Optional

import config
import pdf_parser as P

log = logging.getLogger(__name__)

MAX_OPTIONS = config.MAX_OPTIONS          # 12 (Telegram limit)
MAX_CHARS = 2_000_000                     # hard cap on decoded characters

# ------------------------------------------------------------------ tags
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
         "audio", "video", "object", "embed"}
_BLOCK = {"p", "div", "li", "tr", "td", "th", "table", "thead", "tbody", "tfoot", "section",
          "article", "header", "footer", "main", "aside", "blockquote", "pre", "br", "hr",
          "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "dt", "dd", "figure",
          "figcaption", "form", "fieldset", "legend", "label", "details", "summary"}

_OPTION_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:options?|opts?|choices?|variants?|radio|checkbox)(?:[\s_-]*\d+)?(?:$|[\s_-])"
    r"|(?:^|[\s_-])answers?(?:[\s_-]*\d+)?(?:$|[\s_-])"
    r"|^mcq-(?:option|choice|answer)$|^[a-d]$|(?:विकल्प)", re.IGNORECASE)
_QUESTION_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:questions?|ques|q|mcq|problem|prashn)(?:[\s_-]*\d+)?(?:$|[\s_-])"
    r"|^q\d+$|^question\d+$|(?:प्रश्न)", re.IGNORECASE)
_ANSWER_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:answer|ans|correct[-_]?answer|answer[-_]?key)(?:$|[\s_-])",
                              re.IGNORECASE)
_CORRECT_RE = re.compile(r"correct|right|true|is-?answer|^ans$|^answer$|sahi|सही|सही-उत्तर",
                         re.IGNORECASE)
_WRONG_RE = re.compile(r"wrong|incorrect|false|galat|गलत", re.IGNORECASE)
_EXPL_CLASS_RE = re.compile(r"explanation|explain|solution|sol$|reason|analysis|hint|व्याख्या|हल|विस्तार",
                            re.IGNORECASE)
_EXPL_TEXT_RE = re.compile(
    r"^\s*(?:व्याख्या|उत्तर\s*व्याख्या|हल|Explanation|Solution|Sol\.?|Reason|Analysis)\s*[:\-–—]\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)
_ANS_ANY_RE = re.compile(
    r"(?:सही\s*उत्तर|सही\s*विकल्प|उत्तर|Correct\s*(?:Answer|Option|Ans\.?)|Right\s*Answer|"
    r"Answer|Ans\.?)\s*[:\-–—=]?\s*\(?\s*(?P<tok>[A-La-l])\s*\)?"
    r"(?=[\s\.,;।:\-–—]|$)", re.IGNORECASE)
_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:प्रश्न\s*|Question\s*|Ques\.?\s*|Que\.?\s*|Q\.?\s*)?(?P<num>\d{1,4})\s*[\.\):\-–—]\s*",
    re.IGNORECASE)
_TAG_NUM_RE = re.compile(r"(?:question|ques|q|item|prashn)[\s_-]*(\d{1,4})", re.IGNORECASE)
_OPT_PREFIX_RE = re.compile(r"^\s*[\(\[]?\s*(?P<lab>[A-La-l]|1[0-2]|[1-9])\s*[\)\].:\-–—]\s*(?P<rest>.+)$",
                            re.DOTALL)


# ------------------------------------------------------------------ tree
class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: Optional["Node"] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []
        self.parent = parent

    @property
    def classes(self) -> str:
        return f"{self.attrs.get('class', '')} {self.attrs.get('id', '')}".strip()

    def has_class(self, regex: re.Pattern) -> bool:
        return bool(regex.search(self.classes))

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default) or ""

    def descendants(self, skip_hidden: bool = True) -> Iterable["Node"]:
        for ch in self.children:
            if isinstance(ch, Node):
                if skip_hidden and ch.tag in _SKIP:
                    continue
                yield ch
                yield from ch.descendants(skip_hidden)

    def find_all(self, pred) -> list["Node"]:
        return [n for n in self.descendants() if pred(n)]

    def text(self, sep: str = " ") -> str:
        return render_text(self)

    def __repr__(self) -> str:                       # pragma: no cover - debugging
        return f"<{self.tag} class={self.classes!r}>"


def render_text(node: Node, exclude: Optional[set] = None) -> str:
    """Text of ``node`` (block tags → newlines), skipping ``exclude`` subtrees."""
    exclude = exclude or set()
    parts: list[str] = []

    def walk(n: Node) -> None:
        for ch in n.children:
            if isinstance(ch, str):
                parts.append(ch)
            elif ch.tag in _SKIP or id(ch) in exclude:
                continue
            elif ch.tag == "br":
                parts.append("\n")
            elif ch.tag in ("li",):
                parts.append("\n")
                walk(ch)
                parts.append("\n")
            elif ch.tag in ("td", "th"):
                parts.append("  ")
                walk(ch)
                parts.append("  ")
            elif ch.tag in _BLOCK:
                parts.append("\n")
                walk(ch)
                parts.append("\n")
            else:
                walk(ch)
    walk(node)
    raw = "".join(parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


class _TreeBuilder(HTMLParser):
    def __init__(self, max_nodes: int):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self.stack = [self.root]
        self.nodes = 0
        self.max_nodes = max_nodes
        self.truncated = False

    def _add(self, tag, attrs, close: bool):
        tag = tag.lower()
        if tag in _SKIP and tag not in ("embed",):
            # keep the tag (so its content is skipped) but never descend into it
            node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
            self.stack[-1].children.append(node)
            self.nodes += 1
            if not close:
                self._skip_depth = getattr(self, "_skip_depth", 0) + 1
            return
        if self.nodes >= self.max_nodes:
            self.truncated = True
            return
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes += 1
        if not close and tag not in _VOID and tag not in _SKIP:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self._add(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs, True)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data and self.stack and self.stack[-1].tag not in _SKIP:
            self.stack[-1].children.append(data)


# ------------------------------------------------------------------ data
@dataclass
class HtmlOption:
    text: str
    selected: bool = False
    weak: bool = False
    label: str = ""
    node: Optional[Node] = field(default=None, repr=False, compare=False)


@dataclass
class HtmlQuestion:
    number: int
    question: str
    options: list[HtmlOption]
    answer: Optional[int] = None
    explanation: str = ""
    section: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    result: P.ParseResult
    questions: int = 0
    with_answer: int = 0
    without_answer: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    structural: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.result.questions)


# ------------------------------------------------------------------ validation
class HtmlError(Exception):
    """Unreadable / unsupported file (message is shown to the user)."""


def looks_like_html(name: str, mime: str) -> bool:
    name = (name or "").lower()
    mime = (mime or "").lower()
    return name.endswith((".html", ".htm", ".xhtml")) or mime in (
        "text/html", "application/xhtml+xml")


def decode(raw: bytes) -> str:
    """Bytes → text, respecting BOM/meta charset; never raises for odd bytes."""
    if not raw:
        raise HtmlError("File खाली है।")
    if raw[:5] == b"%PDF-":
        raise HtmlError("यह PDF file है, HTML नहीं — इसे /import में PDF के रूप में भेजें।")
    if raw[:4] == b"PK\x03\x04":
        raise HtmlError("यह ZIP/Office file है, HTML नहीं।")
    if len(raw) > config.HTML_MAX_BYTES:
        raise HtmlError(f"HTML file बहुत बड़ी है ({len(raw):,} bytes)। सीमा "
                        f"{config.HTML_MAX_BYTES:,} bytes है।")
    head = raw[:2048].lower()
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                break
    m = re.search(rb"charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-]+)", head)
    encodings = [m.group(1).decode("ascii", "ignore")] if m else []
    encodings += ["utf-8", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def parse_dom(html: str) -> tuple[Node, _TreeBuilder]:
    builder = _TreeBuilder(config.HTML_MAX_NODES)
    if "<" not in html:
        builder.root.children.append(html)
        return builder.root, builder
    try:
        builder.feed(html)
        builder.close()
    except Exception as exc:                        # malformed markup must never crash
        log.warning("HTML parse recovered from %s", exc)
    return builder.root, builder


# ------------------------------------------------------------------ options
def _is_answer_line(text: str) -> bool:
    t = text.strip()
    return bool(_ANS_ANY_RE.match(t) and len(t) <= 40) or bool(_EXPL_TEXT_RE.match(t))


def _clean_option_text(text: str) -> tuple[str, str]:
    """Option text → (label, text) with a leading "A)" / "(A)" removed."""
    text = text.strip()
    text = re.sub(r"^[\s✓✔☑✅●○•·\-–—]+", "", text)
    m = _OPT_PREFIX_RE.match(text)
    if m and len(m.group("rest").strip()) >= 1:
        return m.group("lab"), m.group("rest").strip()
    return "", text


def _option_from_node(node: Node, *, selected: bool, weak: bool, label: str = "",
                      text: Optional[str] = None) -> Optional[HtmlOption]:
    txt = render_text(node) if text is None else text
    lab, txt = _clean_option_text(txt)
    if not txt or _is_answer_line(txt):
        return None
    return HtmlOption(text=txt, selected=selected, weak=weak, label=label or lab, node=node)


def _selected_signals(node: Node) -> tuple[bool, bool]:
    """(selected, weak) from class/id/data-*/aria-* of an option element."""
    cls = node.classes
    selected, weak = False, False
    if cls and _CORRECT_RE.search(cls) and not _WRONG_RE.search(cls):
        selected = True
    for key in ("data-correct", "data-answer", "data-is-correct", "data-right", "aria-checked"):
        v = node.get(key).strip().lower()
        if v in ("true", "1", "yes", "correct", "right"):
            selected = True
        elif v in ("false", "0", "no"):
            selected = False
    style = node.get("style").lower()
    if not selected and re.search(r"background(?:-color)?\s*:\s*(?:green|lightgreen|#c[0-9a-f]{0,5}"
                                  r"|#d[0-9a-f]d[0-9a-f]{2}|#dfd|#e[0-9a-f]{2}f[0-9a-f]{2})", style):
        selected, weak = True, True
    if not selected and re.search(r"(?:^|;)\s*color\s*:\s*(?:green|#0[0-9a-f]{4,5}|#008000|#0a0|#080)", style):
        selected, weak = True, True
    if selected and _WRONG_RE.search(cls):
        selected = False
    return selected, weak


def _label_for_input(inp: Node, container: Node) -> Optional[Node]:
    iid = inp.get("id")
    if iid:
        for n in container.find_all(lambda x: x.tag == "label" and x.get("for") == iid):
            return n
    p = inp.parent
    while p is not None and p is not container:
        if p.tag == "label":
            return p
        p = p.parent
    parent = inp.parent
    if parent is not None:
        sibs = [c for c in parent.children if isinstance(c, Node)]
        try:
            idx = sibs.index(inp)
        except ValueError:
            idx = -1
        for s in sibs[idx + 1:]:
            if s.tag == "label":
                return s
            if s.tag in ("li", "div", "p", "span") and len(render_text(s)) < 300:
                return s
    return None


def _text_after(inp: Node) -> str:
    """Text that follows an <input> inside its parent (when there is no <label>)."""
    parent = inp.parent
    if parent is None:
        return ""
    kids = list(parent.children)
    try:
        idx = kids.index(inp)
    except ValueError:
        return ""
    parts: list[str] = []
    for k in kids[idx + 1:]:
        if isinstance(k, str):
            parts.append(k)
        elif isinstance(k, Node) and k.tag not in ("input",):
            if k.tag == "input":
                break
            parts.append(render_text(k))
        if len(" ".join(parts)) > 400:
            break
    return " ".join(p for p in parts if p).strip()


def _input_options(container: Node) -> list[HtmlOption]:
    inputs = container.find_all(lambda n: n.tag == "input" and n.get("type").lower() in ("radio", "checkbox"))
    if len(inputs) < 2:
        return []
    out: list[HtmlOption] = []
    for inp in inputs:
        lab = _label_for_input(inp, container)
        selected = "checked" in inp.attrs or inp.get("checked").lower() in ("checked", "true", "1", "yes")
        weak = False
        sel2, weak2 = _selected_signals(inp)
        selected, weak = (selected or sel2), (weak or weak2)
        node = lab if lab is not None else inp.parent
        text = render_text(lab) if lab is not None else _text_after(inp)
        opt = _option_from_node(node, selected=selected, weak=weak,
                                label=inp.get("value"), text=text)
        if opt is None:
            value = inp.get("value").strip()
            if value and not re.fullmatch(r"[A-La-l1-9]|1[0-2]", value):   # real text, not just a letter
                opt = HtmlOption(text=value, selected=selected, weak=weak, node=inp.parent)
        if opt is not None:
            out.append(opt)
    return out


def _element_options(container: Node) -> list[HtmlOption]:
    cands: list[Node] = []
    for n in container.descendants():
        if n.tag in ("input", "table", "tr", "tbody", "thead", "form", "body", "html"):
            continue
        if n.has_class(_EXPL_CLASS_RE) or _is_answer_line(render_text(n)):
            continue
        if n.has_class(_OPTION_CLASS_RE):
            cands.append(n)
    if len(cands) < 2:
        return []
    # innermost matches only
    cands = [c for c in cands if not any(o is not c and _contains(c, o) for o in cands)]
    out: list[HtmlOption] = []
    for c in cands:
        selected, weak = _selected_signals(c)
        text = render_text(c)
        if text.startswith(("✓", "✔", "☑", "✅")):
            selected = True
            weak = weak or not (c.classes and _CORRECT_RE.search(c.classes))
        opt = _option_from_node(c, selected=selected, weak=weak, text=text)
        if opt is not None:
            out.append(opt)
    return out


def _table_options(container: Node) -> list[HtmlOption]:
    if container.tag != "tr":
        return []
    cells = [c for c in container.children if isinstance(c, Node) and c.tag in ("td", "th")]
    if len(cells) < 2:
        return []
    out: list[HtmlOption] = []
    for cell in cells[1:]:
        text = render_text(cell)
        if not text or _is_answer_line(text):
            continue
        if len(text) > 500 or text.count("\n") > 6:          # not an option cell
            return []
        selected, weak = _selected_signals(cell)
        opt = _option_from_node(cell, selected=selected, weak=weak, text=text)
        if opt is not None:
            out.append(opt)
    return out


def _fallback_options(container: Node) -> list[HtmlOption]:
    for lst in container.find_all(lambda n: n.tag in ("ol", "ul")):
        items = [c for c in lst.children if isinstance(c, Node) and c.tag == "li"]
        items = [i for i in items if not i.has_class(_EXPL_CLASS_RE)]
        if len(items) >= 2 and not any(i.find_all(lambda x: x.tag == "input") for i in items):
            out = []
            for i in items:
                selected, weak = _selected_signals(i)
                opt = _option_from_node(i, selected=selected, weak=weak)
                if opt is not None:
                    out.append(opt)
            if len(out) >= 2:
                return out
    return []


def collect_options(container: Node) -> list[HtmlOption]:
    for fn in (_input_options, _element_options, _table_options, _fallback_options):
        try:
            opts = fn(container)
        except Exception:                                    # noqa: BLE001 - never crash
            log.exception("option extraction failed in %s", fn.__name__)
            opts = []
        if len(opts) >= 2:
            return opts
    return []


# ------------------------------------------------------------------ containers
def _contains(outer: Node, inner: Node) -> bool:
    p = inner.parent
    while p is not None:
        if p is outer:
            return True
        p = p.parent
    return False


def _lca(nodes: list[Node]) -> Optional[Node]:
    if not nodes:
        return None
    chain: list[Node] = []
    p = nodes[0].parent
    while p is not None:
        chain.append(p)
        p = p.parent
    for anc in chain:
        if all(_contains(anc, n) for n in nodes):
            return anc
    return None


def _signature(node: Node) -> tuple:
    cls = " ".join(sorted(set((node.get("class") or "").split())))
    cls = re.sub(r"\d+", "#", cls)
    return node.tag, cls


def _strong_containers(root: Node) -> list[Node]:
    """Blocks whose class/id mark them as a question AND that yield options."""
    strong = []
    for n in root.descendants():
        if n.tag in ("html", "body", "table", "tbody", "thead", "form"):
            continue
        if not n.has_class(_QUESTION_CLASS_RE):
            continue
        if len(render_text(n)) < 6:
            continue
        if len(collect_options(n)) >= 2:
            strong.append(n)
    if not strong:
        return []
    groups: dict[tuple, list[Node]] = {}
    for n in strong:
        groups.setdefault(_signature(n), []).append(n)
    best = max(groups.values(), key=len)
    best = [c for c in best if not any(o is not c and _contains(c, o) for o in best)]
    if len(best) >= 2:
        return best
    if not best:
        return []
    # a single block: make sure it is not a wrapper around several question blocks
    wrap = best[0]
    inner = [n for n in wrap.descendants()
             if n is not wrap and n.has_class(_QUESTION_CLASS_RE) and len(collect_options(n)) >= 2]
    if inner:
        inner = [c for c in inner if not any(o is not c and _contains(c, o) for o in inner)]
        groups2: dict[tuple, list[Node]] = {}
        for n in inner:
            groups2.setdefault(_signature(n), []).append(n)
        best2 = max(groups2.values(), key=len)
        if len(best2) >= 2:
            return best2
        return [wrap] if len(collect_options(wrap)) >= 2 and not inner else inner[:1]
    return [wrap]


def _input_containers(root: Node) -> list[Node]:
    """Question blocks found from radio/checkbox groups (no class names needed)."""
    inputs = [n for n in root.descendants()
              if n.tag == "input" and n.get("type").lower() in ("radio", "checkbox")]
    if len(inputs) < 2:
        return []
    by_name: dict[str, list[Node]] = {}
    for i, inp in enumerate(inputs):
        key = inp.get("name") or f"__anon{id(inp.parent)}"
        by_name.setdefault(key, []).append(inp)
    groups = [g for g in by_name.values() if len(g) >= 2]
    if not groups and len(inputs) >= 2:
        groups = [inputs]                          # one quiz, inputs share no name
    containers: list[Node] = []
    for g in groups:
        anc = _lca(g)
        if anc is None:
            continue
        # a common ancestor that swallows the whole page is not a question block
        txt = render_text(anc)
        if len(txt) > 4000 or anc.tag in ("html", "body", "form", "table"):
            # try the parents of each input instead (one block per input group parent)
            anc = _lca([i.parent for i in g if i.parent is not None]) or anc
            if anc is None or anc.tag in ("html", "body", "form", "table"):
                continue
        # a common parent that is only the option list → climb to the question block
        for _ in range(4):
            if anc.tag not in ("ul", "ol") and not anc.has_class(_OPTION_CLASS_RE):
                break
            parent = anc.parent
            if parent is None or parent.tag in ("html", "body", "form"):
                break
            same_inputs = (len(parent.find_all(lambda x: x.tag == "input")) ==
                           len(anc.find_all(lambda x: x.tag == "input")))
            if same_inputs and len(render_text(parent)) > len(render_text(anc)):
                anc = parent
            else:
                break
        containers.append(anc)
    # innermost only, unique
    uniq: list[Node] = []
    for c in containers:
        if all(c is not u for u in uniq):
            uniq.append(c)
    uniq = [c for c in uniq if not any(o is not c and _contains(c, o) for o in uniq)]
    return uniq if len(uniq) >= 2 else (uniq if uniq and len(inputs) >= 2 else [])


def _table_containers(root: Node) -> list[Node]:
    rows = []
    for tr in root.descendants():
        if tr.tag != "tr":
            continue
        cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
        if len(cells) < 3 or len(render_text(tr)) < 6:
            continue
        if all(c.tag == "th" for c in cells):         # header row, not a question
            continue
        rows.append(tr)
    rows = [r for r in rows if len(collect_options(r)) >= 2]
    return rows if len(rows) >= 2 else []


def _sequence_containers(root: Node) -> list[Node]:
    """Flat forms: consecutive option wrappers as siblings, split by input name."""
    units: list[tuple[Node, str, Node]] = []          # (wrapper, input name, input)
    for inp in root.descendants():
        if inp.tag != "input" or inp.get("type").lower() not in ("radio", "checkbox"):
            continue
        w = inp.parent
        hops = 0
        while w is not None and hops < 3 and len(render_text(w)) > 400:
            w, hops = w.parent, hops + 1
        if w is not None:
            units.append((w, inp.get("name"), inp))
    runs: list[list[tuple[Node, str, Node]]] = []
    for u in units:
        if runs and runs[-1][-1][0].parent is u[0].parent and (
                not u[1] or not runs[-1][-1][1] or u[1] == runs[-1][-1][1]):
            runs[-1].append(u)
        else:
            runs.append([u])
    containers: list[Node] = []
    for run in runs:
        if len(run) < 2:
            continue
        parent = run[0][0].parent
        if parent is None:
            continue
        kids = list(parent.children)
        try:
            idx = kids.index(run[0][0])
        except ValueError:
            continue
        prev: list[Node] = []
        j = idx - 1
        while j >= 0 and len(prev) < 2:
            k = kids[j]
            if isinstance(k, str):
                if k.strip():
                    break
            elif any(k is x[0] for r in runs for x in r):
                break
            elif len(render_text(k)) > 600:
                break
            else:
                prev.insert(0, k)
                if re.match(r"^\s*(?:प्रश्न|Question|Ques|Q\.?\s*|\d{1,4}\s*[\.\)])", render_text(k)):
                    break
            j -= 1
        synthetic = Node("div", {"class": "quizbot-sequence"}, None)
        synthetic.children = prev + [u[0] for u in run]      # exact wrappers, document order
        containers.append(synthetic)
    return containers if len(containers) >= 1 else []


def container_candidates(root: Node) -> tuple[list[Node], str]:
    """Question blocks + which strategy found them."""
    for fn, name in ((_strong_containers, "class"), (_input_containers, "inputs"),
                     (_sequence_containers, "sequence"), (_table_containers, "table")):
        try:
            found = fn(root)
        except Exception:                                    # noqa: BLE001
            log.exception("container detection failed in %s", name)
            found = []
        if found:
            order = {id(n): i for i, n in enumerate(root.descendants())}
            found.sort(key=lambda n: order.get(id(n), 0))
            return found, name
    return [], "none"


# ------------------------------------------------------------------ questions
def _answer_from_text(text: str, n_options: int) -> tuple[Optional[int], bool]:
    for m in _ANS_ANY_RE.finditer(text or ""):
        tok = m.group("tok").upper()
        idx = ord(tok) - ord("A")
        if 0 <= idx < n_options:
            return idx, True
    return None, False


def _explanation_node(container: Node) -> Optional[Node]:
    cands = [n for n in container.descendants()
             if n.tag not in ("input",) and n.has_class(_EXPL_CLASS_RE) and len(render_text(n)) > 2]
    if not cands:
        return None
    return cands[0]


def _explanation_text(container: Node, expl_node: Optional[Node], full_text: str) -> str:
    if expl_node is not None:
        t = render_text(expl_node)
        if t:
            return t
    m = _EXPL_TEXT_RE.search(full_text or "")
    return m.group("rest").strip() if m and m.group("rest").strip() else ""


def build_question(container: Node, index: int) -> Optional[HtmlQuestion]:
    options = collect_options(container)
    if len(options) < 2:
        return None
    expl_node = _explanation_node(container)
    exclude = {id(o.node) for o in options if o.node is not None}
    if expl_node is not None:
        exclude.add(id(expl_node))
    qtext = render_text(container, exclude=exclude)

    # remove an "Answer: …" / "Explanation: …" line that lives as plain text
    qtext = _ANS_ANY_RE.sub(" ", qtext)
    m = _EXPL_TEXT_RE.search(qtext)
    if m:
        qtext = qtext[:m.start()] + " " + m.group("rest")
    lines = [ln.strip() for ln in qtext.split("\n")]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"[\.\-–—_=·•]+", ln)]
    qtext = "\n".join(lines).strip()

    number = index + 1
    m = _NUM_PREFIX_RE.match(qtext)
    if m:
        number = int(m.group("num"))
        qtext = qtext[m.end():].strip()
    else:
        tm = _TAG_NUM_RE.search(container.classes)
        if tm:
            number = int(tm.group(1))
    if not qtext:
        qtext = "_(HTML में इस प्रश्न का text नहीं मिला)_"

    selected = [i for i, o in enumerate(options) if o.selected]
    answer: Optional[int] = None
    notes: list[str] = []
    full = render_text(container)
    if len(selected) == 1:
        answer = selected[0]
        if options[answer].weak:
            notes.append("HTML में उत्तर केवल रंग/हाइलाइट से चिह्नित है — जांचें")
    elif len(selected) > 1:
        notes.append(f"HTML में {len(selected)} options सही चिह्नित हैं (multi-answer) — "
                     "bot का quiz poll एक ही सही उत्तर का होता है")
    else:
        idx, found = _answer_from_text(full, len(options))
        if idx is not None:
            answer = idx
            notes.append("उत्तर HTML के 'Answer:' text से लिया गया — जांचें")
        elif found:
            notes.append("HTML का उत्तर text पढ़ा नहीं जा सका")

    explanation = _explanation_text(container, expl_node, full)
    section = ""
    return HtmlQuestion(number=number, question=qtext, options=options, answer=answer,
                        explanation=explanation, section=section, notes=notes)


_SECTION_TAGS = {"h2", "h3", "h4"}
_SECTION_CLASS_RE = re.compile(r"section[-_]?(?:title|heading)|part[-_]?title|भाग", re.IGNORECASE)


def _sections_by_node(root: Node) -> dict[int, str]:
    """id(question block) → the nearest preceding section heading (exact text)."""
    current = ""
    out: dict[int, str] = {}
    for n in root.descendants():
        if n.tag in _SECTION_TAGS or (n.tag in ("div", "p", "span") and n.has_class(_SECTION_CLASS_RE)):
            t = render_text(n)
            if 0 < len(t) <= 120:
                current = t
        out[id(n)] = current
    return out


def find_questions(root: Node, progress: Optional[Callable[[int, int], None]] = None
                   ) -> tuple[list[HtmlQuestion], str]:
    containers, strategy = container_candidates(root)
    sections = _sections_by_node(root)
    out: list[HtmlQuestion] = []
    total = len(containers)
    for i, node in enumerate(containers, 1):
        try:
            q = build_question(node, i - 1)
            if q is not None and node.parent is not None:
                q.section = sections.get(id(node), "")
        except Exception:                                    # noqa: BLE001
            log.exception("question extraction failed")
            q = None
        if q is not None:
            out.append(q)
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)
    return out, strategy


# ------------------------------------------------------------------ text mode
def dom_to_text(root: Node) -> str:
    """Plain-text rendering (the shape a .txt upload would have)."""
    chunks: list[str] = []

    def walk(n: Node) -> None:
        for ch in n.children:
            if isinstance(ch, str):
                t = re.sub(r"\s+", " ", ch)
                if t.strip():
                    chunks.append(t)
            elif ch.tag in _SKIP:
                continue
            elif ch.tag == "br":
                chunks.append("\n")
            elif ch.tag == "li":
                chunks.append("\n• ")
                walk(ch)
                chunks.append("\n")
            elif ch.tag in ("td", "th"):
                chunks.append("  ")
                walk(ch)
                chunks.append("  ")
            elif ch.tag in _BLOCK:
                chunks.append("\n")
                walk(ch)
                chunks.append("\n")
            else:
                walk(ch)
    walk(root)
    text = "".join(chunks)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------------ entry point
def extract(raw: bytes, progress: Optional[Callable[[int, int], None]] = None,
            title: str = "") -> Extraction:
    """HTML bytes → ParseResult (structured) or a text-parsed fallback."""
    html = decode(raw)
    if len(html) > MAX_CHARS:
        raise HtmlError("HTML बहुत बड़ी है — छोटे हिस्सों में भेजें।")
    root, builder = parse_dom(html)
    questions, strategy = find_questions(root, progress=progress)

    ex = Extraction(result=P.ParseResult(), structural=bool(questions))
    res = ex.result
    if builder.truncated:
        ex.notes.append(f"HTML बहुत बड़ी थी — {config.HTML_MAX_NODES} elements के बाद पढ़ना रोका गया; "
                        "बाकी हिस्सा अलग file में भेजें।")

    if not questions:
        text = dom_to_text(root)
        if not text:
            raise HtmlError("HTML में कोई पढ़ने लायक text नहीं मिला।")
        parsed = P.parse_source(text)
        ex.notes.append("HTML में question blocks की पहचान नहीं हुई — file को text की तरह parse किया गया "
                        "(PDF/Text जैसा)। नीचे हर प्रश्न जांचें।")
        ex.result = parsed
        ex.questions = len(parsed.questions)
        ex.with_answer = len(parsed.questions)
        ex.without_answer = [str(e) for e in parsed.errors]
        return ex

    for q in questions:
        if len(q.options) > MAX_OPTIONS:
            res.errors.append(P.ParseError(q.number, f"{len(q.options)} options — Telegram में अधिकतम "
                                                     f"{MAX_OPTIONS} allowed"))
            continue
        if q.answer is None:
            why = "; ".join(q.notes) if q.notes else "उत्तर HTML में स्पष्ट नहीं है"
            res.errors.append(P.ParseError(q.number, "Answer Not Found — " + why))
            ex.without_answer.append(f"प्रश्न {q.number}: {why}")
            continue
        flags = list(q.notes)
        if "text नहीं मिला" in q.question:
            flags.append("प्रश्न का text खाली है")
        res.questions.append(P.ParsedQuestion(
            number=q.number, question=q.question, options=[o.text for o in q.options],
            correct_index=int(q.answer), explanation=q.explanation,
            qtype=P.detect_qtype(q.question) if hasattr(P, "detect_qtype") else "mcq",
            answer_source="html", section=(1 if q.section else 0), section_label=q.section, page=None,
            verify=[], pre_text="", flags=flags))
        if flags:
            res.review.append(f"प्रश्न {q.number}: " + "; ".join(flags))

    ex.questions = len(questions)
    ex.with_answer = len(res.questions)
    if ex.without_answer:
        ex.notes.append(f"{len(ex.without_answer)} प्रश्नों में उत्तर स्पष्ट नहीं था — उन्हें import नहीं किया "
                        "जाएगा (bot कभी अनुमान नहीं लगाता)।")
    if res.review:
        ex.notes.append(f"{len(res.review)} प्रश्न जांचने लायक हैं (नीचे Verification Required देखें)।")
    return ex
