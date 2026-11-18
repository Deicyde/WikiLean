"""Pure text parsers: headings and anchors, layer keys, Lean names, Stacks and arXiv ids.

Nothing here touches the filesystem, so every rule is unit-tested on strings.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------- headings

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")


def lines_of(text: str) -> list[str]:
    """1-based access is `lines[n - 1]`; a trailing CR is dropped from each line."""
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def fence_mask(lines: list[str]) -> list[bool]:
    """True for every line inside (or delimiting) a fenced code block."""
    mask, open_fence = [], None
    for ln in lines:
        m = _FENCE.match(ln)
        if open_fence is None:
            if m:
                open_fence = m.group(1)[0] * 3
                mask.append(True)
                continue
            mask.append(False)
        else:
            mask.append(True)
            if m and m.group(1).startswith(open_fence):
                open_fence = None
    return mask


def heading_text(raw: str) -> str:
    """The rendered text of a heading: links keep their label, emphasis markers and
    backticks go, code spans keep their contents."""
    parts = re.split(r"(`[^`]*`)", raw)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) >= 2:
            out.append(p[1:-1])
        else:
            p = _LINK.sub(r"\1", p)
            p = _HTML.sub("", p)
            out.append(p.replace("*", ""))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _slug_base(text: str) -> str:
    kept = []
    for ch in text.lower():
        cat = unicodedata.category(ch)
        if cat[0] in "LMN" or cat == "Pc" or ch in " -":
            kept.append(ch)
    return "".join(kept).replace(" ", "-")


class Slugger:
    """GitHub's anchor rule (github-slugger): lowercase; drop every character that is not
    a letter, mark, number, connector punctuation, space or hyphen; spaces become hyphens;
    a repeated slug gets `-1`, `-2`, ... in document order."""

    def __init__(self):
        self.seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        base = _slug_base(text)
        result = base
        while result in self.seen:
            self.seen[base] += 1
            result = f"{base}-{self.seen[base]}"
        self.seen[result] = 0
        return result


def headings(lines: list[str]) -> list[dict]:
    """Every ATX heading outside code fences: line (1-based), level, title, anchor."""
    mask = fence_mask(lines)
    slugger = Slugger()
    out = []
    for i, ln in enumerate(lines, 1):
        if mask[i - 1]:
            continue
        m = _HEADING.match(ln)
        if not m:
            continue
        title = heading_text(m.group(2))
        out.append({"line": i, "level": len(m.group(1)), "title": title,
                    "anchor": slugger.slug(title)})
    return out


# ---------------------------------------------------------------- layer keys

_WORD_KEY = r"(?:Layer|Milestone|Stage|Part|Lane|Phase|Level|Step)[ \t]+(?>[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*)"
_CODE_KEY = r"(?>[A-Z]{1,4}[0-9]+(?:\.[0-9]+)*[a-z]?)"
_KEY = re.compile(
    rf"^(?:\*\*)?(?P<key>{_WORD_KEY}|{_CODE_KEY})(?=[ \t]*(?:[:—–)(,]|\.(?:[ \t]|$)|-[ \t]|\*\*|$))"
)


def layer_key(title: str) -> str | None:
    """`Layer 5: foo` -> `Layer 5`; `Y1 — bar` -> `Y1`; `Part A. baz` -> `Part A`.
    The key must be followed by a separator (`:`, dash, `. `, `(`, `)`, `,`) or end the
    heading, so `Layer 3 prerequisites` and `F0.1 constructions` are not layers."""
    m = _KEY.match(title)
    if not m:
        return None
    return re.sub(r"[ \t]+", " ", m.group("key"))


# ---------------------------------------------------------------- Lean names

_SEG = r"[^\W\d][\w'!?]*"
_DOTTED = re.compile(rf"^(?:_root_\.)?{_SEG}(?:\.{_SEG})+$")
_CODE_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
FILE_SUFFIXES = {"lean", "md", "toml", "json", "html", "pdf", "tex", "py", "olean", "txt", "yml", "yaml"}


def backticked_names(line: str) -> list[tuple[str, str | None]]:
    """(name, reject_reason) for every inline code span that is exactly one dotted name."""
    out = []
    for m in _CODE_SPAN.finditer(line):
        body = m.group(1).strip()
        if not _DOTTED.match(body):
            continue
        if body.startswith("_root_."):
            body = body[len("_root_."):]
        last = body.rsplit(".", 1)[-1]
        out.append((body, "file_name" if last in FILE_SUFFIXES else None))
    return out


_LEAN_DECL = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t]*)*"
    r"(?:(?:private|protected|noncomputable|nonrec|partial|unsafe|scoped|local)[ \t]+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class(?:[ \t]+inductive)?|inductive|instance|axiom|opaque)"
    r"(?:[ \t]*\([^)]*\))?[ \t]+(?P<name>[^\s:({\[⦃]+)"
)
_LEAN_NS = re.compile(r"^[ \t]*namespace[ \t]+(?P<name>\S+)")
_LEAN_SECTION = re.compile(r"^[ \t]*(?:noncomputable[ \t]+)?section\b")
_LEAN_END = re.compile(r"^[ \t]*end\b")
_MD_HEAD_IN_DOC = re.compile(r"^[ \t]*(?:/-!)?[ \t]*(#{1,6})[ \t]+(.*?)[ \t]*(?:-/)?[ \t]*$")


def lean_scan(text: str) -> tuple[list[dict], list[dict]]:
    """Scan a Lean file. Returns (declarations, markers).

    declarations: {name (full), line}; namespaces opened by `namespace` and closed by `end`
    are prefixed, `section` scopes add nothing, `_root_.` makes a name absolute.
    markers: {line, title} for each markdown heading inside a `/-! ... -/` module doc."""
    lines = lines_of(text)
    decls, markers, scopes = [], [], []
    depth = 0  # block-comment nesting
    in_moddoc = False
    for i, raw in enumerate(lines, 1):
        code = []
        j = 0
        while j < len(raw):
            if depth:
                if raw.startswith("/-", j):
                    depth += 1
                    j += 2
                elif raw.startswith("-/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        in_moddoc = False
                else:
                    j += 1
                continue
            if raw.startswith("/-", j):
                depth = 1
                in_moddoc = raw.startswith("/-!", j)
                j += 2
                continue
            if raw.startswith("--", j):
                break
            code.append(raw[j])
            j += 1
        if in_moddoc or (raw.lstrip().startswith("/-!")):
            m = _MD_HEAD_IN_DOC.match(raw)
            if m:
                markers.append({"line": i, "title": heading_text(m.group(2).removesuffix("-/"))})
        line = "".join(code)
        if not line.strip():
            continue
        m = _LEAN_NS.match(line)
        if m:
            scopes.append(m.group("name"))
            continue
        if _LEAN_SECTION.match(line):
            scopes.append(None)
            continue
        if _LEAN_END.match(line):
            if scopes:
                scopes.pop()
            continue
        m = _LEAN_DECL.match(line)
        if m:
            name = m.group("name").rstrip(",")
            if name.startswith("_root_."):
                full = name[len("_root_."):]
            else:
                full = ".".join([s for s in scopes if s] + [name])
            decls.append({"name": full, "line": i})
    return decls, markers


# ---------------------------------------------------------------- Stacks and arXiv

_STACKS_KEYWORD = re.compile(r"(?<![A-Za-z])(?:Stacks|Tags?)(?![a-z])|stacks\.math\.columbia\.edu/tag/")
# Not part of a decimal (`1507.01234`, `10.1507`) or of a longer word.
_STACKS_TOKEN = re.compile(r"(?<![0-9A-Za-z_])(?<!\d\.)([0-9A-Z]{4})(?![0-9A-Za-z_]|\.\d)")
_ARXIV_KEYWORD = re.compile(r"arxiv", re.I)
_ARXIV_NEW = re.compile(r"(?<![\d.])(\d{2})(\d{2})\.(\d{4,5})(v\d+)?(?![\d])")
_ARXIV_OLD = re.compile(r"(?<![\w/.-])([a-z]+(?:-[a-z]+)?(?:\.[A-Z]{2})?/\d{7})(v\d+)?(?!\d)")


def stacks_tags(line: str) -> list[tuple[str, str | None]]:
    """(tag, reject_reason) for four-character tokens with a digit that follow the word
    `Stacks` or `Tag` (or a Stacks tag URL) on the same line. All-digit years are rejected."""
    m = _STACKS_KEYWORD.search(line)
    if not m:
        return []
    out = []
    for t in _STACKS_TOKEN.finditer(line, m.start()):
        tag = t.group(1)
        if not any(c.isdigit() for c in tag):
            continue
        if tag.isdigit() and 1900 <= int(tag) <= 2099:
            out.append((tag, "looks_like_year"))
        else:
            out.append((tag, None))
    return out


def arxiv_ids(line: str) -> list[tuple[str, str | None]]:
    """(id without version, reject_reason) on lines that say arXiv."""
    if not _ARXIV_KEYWORD.search(line):
        return []
    out = []
    for m in _ARXIV_NEW.finditer(line):
        yy, mm, num = m.group(1), m.group(2), m.group(3)
        ident = f"{yy}{mm}.{num}"
        ok = 1 <= int(mm) <= 12 and (len(num) == 5) == (int(yy + mm) >= 1501) and int(yy + mm) >= 704
        out.append((ident, None if ok else "invalid_arxiv_id"))
    for m in _ARXIV_OLD.finditer(line):
        out.append((m.group(1), "old_style_arxiv_id"))
    return out
