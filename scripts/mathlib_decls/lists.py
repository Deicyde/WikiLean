"""The Lean names on Mathlib's curated lists, as trough copies them (`data/mathlib/yaml/`).

Standard library only, so this reads the subset of YAML those four files use rather than YAML:
- `100.yaml`, `1000.yaml`: top-level entries (`1:`, `Q11518:`) whose `decl:` or `statement:`
  names one declaration and whose `decls:` lists several (block `- x` items or `[x, y]`);
- `overview.yaml`, `undergrad.yaml`: nested mappings whose leaf values are names, a value on
  the line after its key included (quoted or plain; the body of a `>`/`|` block is not one).
Scalars may be plain, '…' (with '' for a quote) or "…"; a comment after a value is dropped, and
a value that is only a comment is no value.
"""

from __future__ import annotations

import re

LISTS = {"100": "100.yaml", "1000": "1000.yaml", "overview": "overview.yaml", "undergrad": "undergrad.yaml"}
ENTRY_LISTS = {"100", "1000"}
NAME_FIELDS = {"decl", "statement"}
# A folded or literal block header (`>`, `>-`, `|2+`, …): its body is prose, and so is it.
BLOCK = re.compile(r"[>|][0-9]*[-+]?")


def scalar(text: str) -> str | None:
    """The value of one YAML scalar written on a line, or None when it is empty or unclosed."""
    t = text.strip()
    if not t or t.startswith("#"):  # a comment-only value is no value, not the text of the comment
        return None
    if t[0] == "'":
        out, i = [], 1
        while i < len(t):
            if t[i] == "'":
                if t[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                rest = t[i + 1:].strip()
                return "".join(out) or None if not rest or rest.startswith("#") else None
            out.append(t[i])
            i += 1
        return None
    if t[0] == '"':
        m = re.match(r'^"((?:[^"\\]|\\.)*)"\s*(#.*)?$', t)
        return re.sub(r"\\(.)", r"\1", m.group(1)) or None if m else None
    t = re.split(r"\s+#", t, maxsplit=1)[0].strip()
    return t or None


def _key_value(body: str) -> tuple[str, str] | None:
    """`key: value` (key plain or quoted) -> (key, raw value); None if the line is no mapping."""
    m = re.match(r"""^('(?:[^']|'')*'|"(?:[^"\\]|\\.)*")\s*:(?:\s+(.*))?$""", body)
    if m:
        return scalar(m.group(1)) or "", m.group(2) or ""
    m = re.match(r"^([^'\"#\-\s][^:]*?)\s*:(?:\s+(.*))?$", body)
    return (m.group(1), m.group(2) or "") if m else None


def entries(list_name: str, text: str) -> list[dict]:
    """[{list, entry, value, line}] for every name-bearing value, in file order. `entry` is the
    top-level key of a 100/1000 entry (a number or a Wikidata QID), else None."""
    out: list[dict] = []
    flat = list_name in ENTRY_LISTS
    entry, in_list, pending = None, False, False
    for n, raw in enumerate(text.split("\n"), 1):
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        def add(v):
            if v is not None:
                out.append({"list": list_name, "entry": entry if flat else None, "value": v, "line": n})

        if flat:
            if indent == 0:
                kv = _key_value(body)
                entry, in_list = (kv[0] if kv else None), False
                continue
            if in_list and body.startswith("-"):
                add(scalar(body[1:]))
                continue
            kv = _key_value(body)
            if kv is None:
                continue  # a folded or quoted block's continuation
            in_list = False
            field, value = kv
            if field in NAME_FIELDS:
                add(scalar(value))
            elif field == "decls":
                v = value.strip()
                if not v:
                    in_list = True
                elif v.startswith("[") and v.endswith("]"):
                    for part in v[1:-1].split(","):
                        add(scalar(part))
            continue
        kv = _key_value(body)
        if kv is None:
            if pending and not body.startswith("-"):
                add(scalar(body))  # the value of the key on the line above, quoted or plain
            pending = False
            continue
        _key, value = kv
        v = scalar(value)
        if v is not None and BLOCK.fullmatch(v):
            pending = False  # `key: >-` or `key: |`: the lines below are prose, never a name
            continue
        pending = v is None
        add(v)
    return out


def classify(value: str) -> tuple[str, str]:
    """('decl', name) | ('module', module name) | ('reject', reason) for one list value."""
    if "://" in value:
        return "reject", "not_lean_name"
    m = re.fullmatch(r"((?:[A-Za-z0-9_]+/)+[A-Za-z0-9_]+)\.html", value)
    if m:
        return "module", m.group(1).replace("/", ".")
    if re.search(r"\s", value) or value.startswith(".") or value.endswith("."):
        return "reject", "not_lean_name"
    return "decl", value
