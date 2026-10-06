"""Native WikiLean review items containing lossless, single-record FCA links.

The review item is application data. Only each link's ``annotation`` is an FCA
record. Legacy conversion is explicit; read helpers never rewrite stored data.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from fractions import Fraction
import json
from pathlib import Path
import re
import struct
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

ANCHOR_PROFILE = "urn:wikilean:anchor:v3"
ANCHOR_FIELDS = ("type", "value", "section", "snippet", "from", "from_snippet",
                 "to", "to_snippet", "to_math", "to_alttext")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "wiki/src/vendor/fca/companion.schema.json"
LEGACY_FIELDS = frozenset({"mathlib", "decl", "module", "anchor", "anchors"})
STATUSES = frozenset({"formalized", "partial", "not_formalized", "rejected"})
_JS_SAFE = "-_.!~*'()"


class NativeAnnotationError(ValueError):
    """Native input cannot be consumed without changing or losing its meaning."""


def is_native(item: Any) -> bool:
    return isinstance(item, dict) and ("links" in item or "unlinked" in item)


def _strict_json(value: Any) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise NativeAnnotationError(f"invalid JSON: {exc}") from exc
    # json.dumps otherwise silently coerces integer object keys.
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise NativeAnnotationError("JSON object keys must be strings")
            for member in node.values():
                visit(member)
        elif isinstance(node, list):
            for member in node:
                visit(member)
        elif node is not None and not isinstance(node, (str, bool, int, float)):
            raise NativeAnnotationError("unsupported JSON value")
    visit(value)


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def validate_fca(annotation: Any) -> None:
    """Validate the vendored reference schema and its two numeric invariants."""
    _strict_json(annotation)
    problem = next(_validator().iter_errors(annotation), None)
    if problem is not None:
        raise NativeAnnotationError(f"FCA {problem.json_path}: {problem.message}")
    location = annotation.get("location", {})
    mode = annotation["source"]["type"]
    if mode == "pdf" and "rect" in location:
        x, y, width, height = (Fraction(str(value)) for value in location["rect"])
        if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise NativeAnnotationError("FCA PDF rectangle has invalid extents or bounds")
    if mode == "latex" and "lines" in location:
        start, end = location["lines"]
        if start > end:
            raise NativeAnnotationError("FCA LaTeX line interval is reversed")


def _absolute(uri: Any) -> bool:
    if not isinstance(uri, str) or not uri or re.search(r"[\s\x00-\x1f\x7f-\x9f]", uri):
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", uri))


def _safe_storage_numbers(value: Any) -> None:
    if isinstance(value, dict):
        for member in value.values():
            _safe_storage_numbers(member)
    elif isinstance(value, list):
        for member in value:
            _safe_storage_numbers(member)
    elif (type(value) is int or isinstance(value, float) and value.is_integer()) and abs(value) > 2**53 - 1:
        raise NativeAnnotationError("integer exceeds lossless JavaScript storage range")


def _validate_base(span: dict, references: list[str]) -> None:
    base = span.get("base_uri")
    if "base_uri" in span and not _absolute(base):
        raise NativeAnnotationError("base_uri must be an absolute URI reference")
    if any(not _absolute(uri) for uri in references) and base is None:
        raise NativeAnnotationError("relative FCA references require a durable base_uri")


def validate_items(items: Any, *, title: str | None = None, revid: int | None = None) -> None:
    """Validate native application items without imposing IDs on FCA itself."""
    _strict_json(items)
    if not isinstance(items, list):
        raise NativeAnnotationError("work items must be an array")
    link_ids: set[str] = set()
    item_ids: set[str] = set()
    for item in items:
        if not is_native(item) or not isinstance(item.get("links"), list) or not isinstance(item.get("unlinked"), list):
            raise NativeAnnotationError("native work items require links and unlinked arrays")
        _safe_storage_numbers(item)
        if LEGACY_FIELDS & item.keys():
            raise NativeAnnotationError("native work item contains live legacy link fields")
        if item.get("status") not in STATUSES:
            raise NativeAnnotationError("invalid native review status")
        if "id" in item:
            identity = item["id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{12}", identity) or identity in item_ids:
                raise NativeAnnotationError("invalid or duplicate work-item id")
            item_ids.add(identity)
        for link in item["links"]:
            _safe_storage_numbers(link)
            if not isinstance(link, dict) or set(link) - {"id", "annotation", "base_uri"}:
                raise NativeAnnotationError("invalid native link wrapper")
            identity = link.get("id")
            if not isinstance(identity, str) or not identity or len(identity) > 256 or identity in link_ids:
                raise NativeAnnotationError("invalid or duplicate internal link id")
            link_ids.add(identity)
            annotation = link.get("annotation")
            validate_fca(annotation)
            references = [annotation["source"]["uri"], annotation["target"]]
            if "$schema" in annotation:
                references.append(annotation["$schema"])
            _validate_base(link, references)
            _validate_pin(annotation["source"]["uri"], link.get("base_uri"), None if item["status"] == "rejected" else title, revid)
        for span in item["unlinked"]:
            _safe_storage_numbers(span)
            if not isinstance(span, dict) or set(span) - {"source", "location", "base_uri"}:
                raise NativeAnnotationError("invalid unlinked source selection")
            # The sentinel exists only inside this validation call, never in data.
            selection = {key: value for key, value in span.items() if key != "base_uri"}
            validate_fca({**selection, "target": "urn:wikilean:selection-validation"})
            _validate_base(span, [span["source"]["uri"]])
            _validate_pin(span["source"]["uri"], span.get("base_uri"), None if item["status"] == "rejected" else title, revid)


def _validate_pin(uri: str, base: str | None, title: str | None, revid: int | None) -> None:
    if title is None:
        return
    wikipedia_source_uri(title, revid)
    try:
        source = urlsplit(urljoin(base or "", uri))
        query = parse_qs(source.query, keep_blank_values=True)
        own = (source.scheme == "https" and source.hostname == "en.wikipedia.org"
               and source.port in (None, 443) and source.path == "/w/index.php"
               and query.get("title", [""])[0].replace("_", " ") == title.replace("_", " "))
    except ValueError:
        return  # Unfamiliar lexical references stay opaque.
    if own and query.get("oldid", [""])[0] != str(revid):
        raise NativeAnnotationError("article occurrence source revision differs from article pin")


def occurrences(item: dict) -> list[dict]:
    """Return native source selections, retaining base and explicit absence."""
    spans = []
    for link in item.get("links", []):
        if not isinstance(link, dict) or not isinstance(link.get("annotation"), dict):
            continue
        annotation = link["annotation"]
        span = {key: annotation[key] for key in ("source", "location") if key in annotation}
        if "base_uri" in link:
            span["base_uri"] = link["base_uri"]
        spans.append(span)
    spans.extend(item.get("unlinked", []))
    return spans


def anchors(item: dict, *, source_uri: str | None = None,
            require_source: bool = False) -> list[dict | None]:
    """Resolve only WikiLean's documented generic selector; unknown stays unknown."""
    if not is_native(item):
        raw = item.get("anchors") or [item.get("anchor", {})]
        return [span if isinstance(span, dict) else None for span in raw]
    result = []
    for span in occurrences(item):
        source = span.get("source", {})
        location = span.get("location")
        if require_source:
            actual = resolved_web_uri(source.get("uri", ""), span.get("base_uri")) if isinstance(source, dict) else None
            if source_uri is None or actual != source_uri:
                result.append(None)
                continue
        if ("location" not in span and isinstance(source, dict)
                and source.get("type") in {"generic", "html"}):
            result.append({"type": "whole_resource"})
            continue
        if (isinstance(source, dict) and source.get("type") == "generic"
                and isinstance(location, dict) and location.get("profile") == ANCHOR_PROFILE
                and isinstance(location.get("anchor"), dict)):
            anchor = location["anchor"]
            malformed = any(key in anchor and not isinstance(anchor[key], str) for key in ANCHOR_FIELDS)
            if anchor.get("type") in {"math_alttext", "theorem_box"} and not anchor.get("value"):
                malformed = True
            if anchor.get("type") == "prose_range" and not (anchor.get("section") is not None
                    and (anchor.get("from") or anchor.get("from_snippet"))):
                malformed = True
            result.append(None if malformed else anchor)
        else:
            result.append(None)
    return result


def resolved_web_uri(reference: str, base: str | None = None) -> str | None:
    """Navigation projection only; the stored lexical reference never changes."""
    try:
        destination = urlsplit(urljoin(base or "", reference))
        if (destination.scheme not in {"http", "https"} or not destination.hostname
                or destination.username or destination.password or "\\" in reference):
            return None
        host = destination.hostname
        if any(char.isspace() for char in host):
            return None
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        port = destination.port
        if port is not None and port != (443 if destination.scheme == "https" else 80):
            host += f":{port}"
        return urlunsplit((destination.scheme, host,
            quote(destination.path or "/", safe="/:@-._~!$&'()*+,;=%"),
            quote(destination.query, safe="/?:@-._~!$&'()*+,;=%"),
            quote(destination.fragment, safe="/?:@-._~!$&'()*+,;=%")))
    except (UnicodeError, ValueError):
        return None


def formal_links(item: dict) -> list[dict]:
    """Public link labels with navigation restricted to HTTP(S)."""
    result = []
    for link in item.get("links", []):
        target = link.get("annotation", {}).get("target")
        if not isinstance(target, str):
            continue
        fragment = target.partition("#")[2]
        try:
            label = unquote(fragment, errors="strict") if fragment else target
        except UnicodeError:
            label = fragment
        record = {"target": target, "label": label}
        url = resolved_web_uri(target, link.get("base_uri"))
        if url:
            record["url"] = url
        result.append(record)
    return result

def primary_anchor(item: dict) -> dict:
    if is_native(item):
        return next((anchor for anchor in anchors(item) if anchor is not None), {})
    anchor = item.get("anchor")
    if isinstance(anchor, dict):
        return anchor
    plural = item.get("anchors")
    return plural[0] if isinstance(plural, list) and plural and isinstance(plural[0], dict) else {}


def stable_native(value: Any) -> str:
    """Opaque signature codec shared with TS, not a JSON storage encoding.

    Numbers use IEEE754 bytes so Python and JavaScript exponent formatting,
    integral floats and signed zero cannot change a review identity.
    """
    if isinstance(value, list):
        return "[" + ",".join(stable_native(member) for member in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + stable_native(value[key])
                               for key in sorted(value)) + "}"
    if type(value) in (int, float):
        return "#" + struct.pack(">d", float(value or 0)).hex()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def anchor_sig(item: dict) -> str:
    if is_native(item):
        spans = occurrences(item)
        first = spans[0] if spans else {}
        return stable_native(["native", first.get("source"), first.get("location"), first.get("base_uri")])
    anchor = primary_anchor(item)
    return json.dumps([anchor.get(key) for key in ("type", "section", "snippet", "value", "from")], sort_keys=True)


def preserve_native_items(existing: list[dict], produced: list[dict]) -> list[dict]:
    """Keep opaque fields when an agent edits native data; refuse lost links.

    Agents may mark a review item rejected, or change a known link in place.
    Dropping its internal identity is not an authorized deletion operation.
    This is never used for human editor saves or to convert legacy objects.
    """
    def identity(item: dict) -> tuple:
        if isinstance(item.get("id"), str):
            return ("id", item["id"])
        return ("selection", anchor_sig(item))

    protected = [item for item in existing if is_native(item) and item.get("provenance") != "human"]
    by_id = {identity(item): item for item in protected}
    if len(by_id) != len(protected):
        raise NativeAnnotationError("ambiguous native work items require stable review IDs before agent edits")

    def overlay(old: Any, new: Any) -> Any:
        if isinstance(old, dict) and isinstance(new, dict):
            return {**deepcopy(old), **{key: overlay(old.get(key), value) for key, value in new.items()}}
        return deepcopy(new)

    result, seen = [], set()
    for fresh in produced:
        key = identity(fresh)
        old = by_id.get(key)
        if old is None:
            # The producer may just have assigned an ID to a migrated item
            # that previously had none. Its source signature identifies it.
            key = ("selection", anchor_sig(fresh))
            old = by_id.get(key)
        if old is None:
            result.append(deepcopy(fresh))
            continue
        seen.add(key)
        if not is_native(fresh):
            raise NativeAnnotationError("agent replaced native data with a legacy object")
        old_links = {link["id"]: link for link in old["links"]}
        fresh_links = fresh.get("links", [])
        if not isinstance(fresh_links, list) or any(not isinstance(link, dict) for link in fresh_links):
            raise NativeAnnotationError("agent returned malformed native links")
        new_ids = {link.get("id") for link in fresh_links}
        if old_links.keys() - new_ids:
            raise NativeAnnotationError("agent dropped a native link; retain it or reject the review item")
        merged = overlay(old, fresh)
        merged["links"] = [overlay(old_links.get(link.get("id")), link) for link in fresh_links]
        # Unlinked selections have no internal link identity. Agents must keep
        # their full source selection, either unlinked or copied into a link.
        # Explicit location edits belong to the editor/proposal flow; a model
        # dropping an opaque PDF/LaTeX/generic selection must not delete it.
        current_spans = occurrences(merged)
        for old_span in old["unlinked"]:
            if old_span not in current_spans:
                raise NativeAnnotationError("agent changed or dropped an unlinked selection; copy its complete source selection")
        result.append(merged)
    if by_id.keys() - seen:
        raise NativeAnnotationError("agent dropped a native review item; mark it rejected explicitly")
    return result


def first_target(item: dict) -> str | None:
    if not is_native(item):
        return None
    for link in item.get("links", []):
        annotation = link.get("annotation", {})
        target = annotation.get("target")
        if isinstance(target, str):
            return urljoin(link.get("base_uri", ""), target)
    return None


def wikipedia_source_uri(title: str, revid: int) -> str:
    if not isinstance(title, str) or not title.strip() or type(revid) is not int or not 0 < revid <= 2**53 - 1:
        raise NativeAnnotationError("migration requires a title and pinned Wikipedia revision")
    return f"https://en.wikipedia.org/w/index.php?title={quote(title.replace(' ', '_'), safe=_JS_SAFE)}&oldid={revid}"


def migrate_items(items: list[dict], *, title: str, revid: int) -> tuple[list[dict], list[dict]]:
    """Explicit one-way conversion; preserve the entire original in legacy."""
    source_uri = wikipedia_source_uri(title, revid)
    _strict_json(items)
    migrated, diagnostics = [], []
    for index, old in enumerate(items):
        if not isinstance(old, dict):
            raise NativeAnnotationError(f"item {index} is not an object")
        if is_native(old):
            migrated.append(deepcopy(old))
            continue
        item = {key: deepcopy(value) for key, value in old.items() if key not in LEGACY_FIELDS}
        item.update(links=[], unlinked=[], legacy=deepcopy(old))
        raw_spans = old.get("anchors") if isinstance(old.get("anchors"), list) and old["anchors"] else [old.get("anchor", {})]
        mathlib = old.get("mathlib") if isinstance(old.get("mathlib"), dict) else {}
        if "match_kind" not in item and isinstance(mathlib.get("match_kind"), str):
            item["match_kind"] = mathlib["match_kind"]
        decl, module = mathlib.get("decl") or old.get("decl"), mathlib.get("module") or old.get("module")
        target = None
        if (isinstance(decl, str) and decl.strip() and isinstance(module, str) and module.strip()
                and all(part and not re.search(r"[\s/\\#?]", part) for part in module.split("."))):
            target = "https://leanprover-community.github.io/mathlib4_docs/" + "/".join(quote(part, safe=_JS_SAFE) for part in module.split(".")) + ".html#" + quote(decl, safe=_JS_SAFE)
        if target is None:
            diagnostics.append({"item": index, "code": "NO_FORMAL_TARGET"})
        elif old.get("status") == "not_formalized":
            diagnostics.append({"item": index, "code": "STATUS_TARGET_CONFLICT"})
        if not old.get("anchor") and not (isinstance(old.get("anchors"), list) and old["anchors"]):
            diagnostics.append({"item": index, "code": "MISSING_ANCHOR"})
        for anchor_index, anchor in enumerate(raw_spans):
            valid = isinstance(anchor, dict) and all(key not in anchor or isinstance(anchor[key], str) for key in ANCHOR_FIELDS)
            if not valid:
                diagnostics.append({"item": index, "anchor": anchor_index, "code": "INVALID_ANCHOR"})
            selector = {key: deepcopy(anchor[key]) for key in ANCHOR_FIELDS if key in anchor} if valid else {}
            selection = {"source": {"type": "generic", "uri": source_uri},
                         "location": {"profile": ANCHOR_PROFILE, "anchor": selector,
                                      "resolution": "not_attempted" if valid else "unresolved"}}
            if target is None or not valid:
                item["unlinked"].append(selection)
            else:
                identity = f"{old.get('id') or 'item' + str(index)}:link:{anchor_index}"
                item["links"].append({"id": identity, "annotation": {**selection, "target": target}})
        migrated.append(item)
    validate_items(migrated, title=title, revid=revid)
    return migrated, diagnostics


def repin_items(items: list[dict], *, title: str, old_revid: int, new_revid: int) -> tuple[list[dict], list[dict]]:
    """Make an auditable AI-owned source update; never rewrite human items."""
    old_uri, new_uri = wikipedia_source_uri(title, old_revid), wikipedia_source_uri(title, new_revid)
    validate_items(items, title=title, revid=old_revid)
    result, changes = deepcopy(items), []
    for index, item in enumerate(result):
        if not is_native(item) or item.get("status") == "rejected":
            continue
        for span_index, span in enumerate(occurrences(item)):
            source = span.get("source", {})
            previous = source.get("uri")
            if resolved_web_uri(previous, span.get("base_uri")) != old_uri:
                continue
            if item.get("provenance") == "human":
                raise NativeAnnotationError("human source revision update requires a moderator source-revision save")
            source["uri"] = new_uri
            changes.append({"item": item.get("id", index), "occurrence": span_index, "from": previous, "to": new_uri})
    validate_items(result, title=title, revid=new_revid)
    return result, changes
