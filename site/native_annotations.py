"""Native WikiLean review items containing lossless, single-record Literature Links annotations.

The review item is application data. Only each link's ``annotation`` is a Literature Links
record. Legacy conversion is explicit; read helpers never rewrite stored data.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
import struct
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

ANCHOR_PROFILE = "urn:wikilean:anchor:v3"
ANCHOR_FIELDS = ("type", "value", "section", "snippet", "from", "from_snippet",
                 "to", "to_snippet", "to_math", "to_alttext")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "wiki/src/vendor/literature-links/companion.schema.json"
LEGACY_FIELDS = frozenset({"mathlib", "decl", "module", "anchor", "anchors"})
STATUSES = frozenset({"formalized", "partial", "not_formalized", "rejected"})
MATHLIB_REPOSITORY = "https://github.com/leanprover-community/mathlib4"
MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs/"
_JS_SAFE = "-_.!~*'()"


class NativeAnnotationError(ValueError):
    """Native input cannot be consumed without changing or losing its meaning."""


def is_native(item: Any) -> bool:
    return isinstance(item, dict) and ("links" in item or "unlinked" in item)


def normalize(annotation: dict) -> dict:
    """literature_links.companion.normalize, mirrored so this pipeline needs no package.

    A copy with a string target as {"uri": ...} and a string status as
    {"value": ...}, so readers handle one shape.
    """
    if not isinstance(annotation, dict):
        raise TypeError("normalize takes one annotation object")
    result = deepcopy(annotation)
    if isinstance(result.get("target"), str):
        result["target"] = {"uri": result["target"]}
    if isinstance(result.get("status"), str):
        result["status"] = {"value": result["status"]}
    return result


def _target(annotation: Any) -> dict:
    target = normalize(annotation).get("target") if isinstance(annotation, dict) else None
    return target if isinstance(target, dict) else {}


def target_uri(annotation: Any) -> str | None:
    """The formal reference of a record; None when it has no target."""
    uri = _target(annotation).get("uri")
    return uri if isinstance(uri, str) else None


def target_pin(annotation: Any) -> dict | None:
    pin = _target(annotation).get("pin")
    return pin if isinstance(pin, dict) and isinstance(pin.get("type"), str) else None


def mathlib_target(module: str, decl: str) -> dict:
    """WikiLean's explicit module+declaration link: the docs URL plus a repository pin."""
    uri = MATHLIB_DOCS + "/".join(quote(part, safe=_JS_SAFE) for part in module.split(".")) + ".html#" + quote(decl, safe=_JS_SAFE)
    return {"uri": uri, "pin": {"type": "repository", "url": MATHLIB_REPOSITORY, "module": module, "declaration": decl}}


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


def validate_link_record(annotation: Any) -> None:
    """Validate the vendored reference schema and its two numeric invariants."""
    _strict_json(annotation)
    problem = next(_validator().iter_errors(annotation), None)
    if problem is not None:
        raise NativeAnnotationError(f"Literature Links {problem.json_path}: {problem.message}")
    location = annotation.get("location", {})
    if "profile" in location:
        return  # Application-defined in every mode; the built-in checks do not apply.
    mode = annotation["source"]["type"]
    if mode == "pdf" and "rect" in location:
        # Double sums, as the specification says, so every language agrees.
        x, y, width, height = (float(value) for value in location["rect"])
        if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise NativeAnnotationError("Literature Links PDF rectangle has invalid extents or bounds")
    if mode == "latex" and "lines" in location:
        start, end = location["lines"]
        if start > end:
            raise NativeAnnotationError("Literature Links LaTeX line interval is reversed")


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
        raise NativeAnnotationError("relative Literature Links references require a durable base_uri")


def validate_items(items: Any, *, title: str | None = None, revid: int | None = None) -> None:
    """Validate native application items without imposing IDs on Literature Links itself."""
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
            validate_link_record(annotation)
            uri = target_uri(annotation)
            # A link is an occurrence with a formal destination; a record without one belongs in unlinked.
            if uri is None:
                raise NativeAnnotationError("native link annotation requires a formal target; keep targetless selections in unlinked")
            references = [annotation["source"]["uri"], uri]
            if "$schema" in annotation:
                references.append(annotation["$schema"])
            _validate_base(link, references)
            _validate_pin(annotation["source"]["uri"], link.get("base_uri"), None if item["status"] == "rejected" else title, revid)
        for span in item["unlinked"]:
            _safe_storage_numbers(span)
            if not isinstance(span, dict) or set(span) - {"source", "location", "base_uri", "extensions"}:
                raise NativeAnnotationError("invalid unlinked source selection")
            validate_link_record({key: value for key, value in span.items() if key != "base_uri"})
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
        span = {key: annotation[key] for key in ("source", "location", "extensions") if key in annotation}
        if "base_uri" in link:
            span["base_uri"] = link["base_uri"]
        spans.append(span)
    spans.extend(item.get("unlinked", []))
    return spans


def anchors(item: dict, *, source_uri: str | None = None,
            require_source: bool = False) -> list[dict | None]:
    """Resolve only WikiLean's documented anchor profile; unknown stays unknown."""
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
        if "location" not in span and isinstance(source, dict) and source.get("type") == "html":
            result.append({"type": "whole_resource"})
            continue
        if not (isinstance(source, dict) and source.get("type") == "html" and isinstance(location, dict)):
            result.append(None)
            continue
        if location.get("profile") == ANCHOR_PROFILE and isinstance(location.get("anchor"), dict):
            result.append(_well_formed_anchor(location["anchor"]))
        elif "profile" in location:
            result.append(None)
        else:
            # A built-in Literature Links location. WikiLean's own records keep the
            # editing key beside it; any other record is read from the location.
            editing_key = _anchor_extension(span)
            result.append(_well_formed_anchor(editing_key) if isinstance(editing_key, dict) else derive_anchor(location))
    return result


def _well_formed_anchor(anchor: dict) -> dict | None:
    malformed = any(key in anchor and not isinstance(anchor[key], str) for key in ANCHOR_FIELDS)
    if anchor.get("type") in {"math_alttext", "theorem_box"} and not anchor.get("value"):
        malformed = True
    if anchor.get("type") == "prose_range" and not (anchor.get("section") is not None
            and (anchor.get("from") or anchor.get("from_snippet"))):
        malformed = True
    return None if malformed else anchor


def _anchor_extension(record: dict) -> Any:
    extensions = record.get("extensions")
    wikilean = extensions.get("org.wikilean") if isinstance(extensions, dict) else None
    return wikilean.get("anchor") if isinstance(wikilean, dict) else None


def derive_anchor(location: dict) -> dict | None:
    """Read a built-in HTML location as a renderer anchor: a quotation, a formula or an element id."""
    from anchor_resolve import parse_math_selector
    quote = location.get("quote")
    if isinstance(quote, dict) and isinstance(quote.get("exact"), str) and quote["exact"]:
        anchor = {"type": "quote", "exact": quote["exact"]}
        for key in ("prefix", "suffix"):
            if isinstance(quote.get(key), str):
                anchor[key] = quote[key]
        return anchor
    css = location.get("css")
    if isinstance(css, str):
        value = parse_math_selector(css)
        return {"type": "math_alttext", "value": value} if value else None
    fragment = location.get("fragment")
    if isinstance(fragment, str) and fragment:
        return {"type": "fragment", "value": fragment}
    return None


def resolve_locations(items: list[dict], html: str) -> tuple[dict[str, int], list[dict]]:
    """Replace WikiLean profile locations by built-in ones resolved against the pinned article HTML.

    The editing key moves to ``extensions["org.wikilean"]["anchor"]``, so the
    editor and the renderers keep working from it, while any Literature Links
    reader can follow the stored quotation or selector. A location that cannot be
    resolved keeps the profile form with ``resolution`` set to why. Mutates
    ``items`` in place; returns per-outcome counts and one diagnostic per
    unresolved occurrence.
    """
    from anchor_resolve import resolve, text_layer
    layer = text_layer(html)
    counts: dict[str, int] = {}
    diagnostics: list[dict] = []
    for index, item in enumerate(items):
        if not is_native(item):
            continue
        records = [("link", k, link.get("annotation")) for k, link in enumerate(item.get("links", []))
                   if isinstance(link, dict)]
        records += [("unlinked", k, span) for k, span in enumerate(item.get("unlinked", []))]
        for kind, position, record in records:
            if not isinstance(record, dict):
                continue
            source, location = record.get("source"), record.get("location")
            if (not isinstance(source, dict) or source.get("type") != "html"
                    or not isinstance(location, dict) or location.get("profile") != ANCHOR_PROFILE
                    or not isinstance(location.get("anchor"), dict)):
                continue
            anchor = location["anchor"]
            resolved, how = resolve(anchor, html, layer) if _well_formed_anchor(anchor) else (None, "malformed")
            counts[how] = counts.get(how, 0) + 1
            if resolved is None:
                location["resolution"] = "unsupported" if how == "unsupported" else "unresolved"
                diagnostics.append({"item": index, kind: position, "code": "LOCATION_UNRESOLVED", "reason": how})
                continue
            record["location"] = resolved
            extensions = record.get("extensions") if isinstance(record.get("extensions"), dict) else {}
            wikilean = dict(extensions["org.wikilean"]) if isinstance(extensions.get("org.wikilean"), dict) else {}
            wikilean["anchor"] = deepcopy(anchor)
            record["extensions"] = {**extensions, "org.wikilean": wikilean}
    return counts, diagnostics


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
        annotation = link.get("annotation", {}) if isinstance(link, dict) else {}
        target = target_uri(annotation)
        if target is None:
            continue
        pin = target_pin(annotation)
        fragment = target.partition("#")[2]
        if pin and isinstance(pin.get("declaration"), str) and pin["declaration"]:
            label = pin["declaration"]
        else:
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
        # A selection is its source, location and base; the editing key beside a
        # built-in location travels with it but is not part of the comparison.
        selection = lambda span: {key: value for key, value in span.items() if key != "extensions"}
        current_spans = [selection(span) for span in occurrences(merged)]
        for old_span in old["unlinked"]:
            if selection(old_span) not in current_spans:
                raise NativeAnnotationError("agent changed or dropped an unlinked selection; copy its complete source selection")
        result.append(merged)
    if by_id.keys() - seen:
        raise NativeAnnotationError("agent dropped a native review item; mark it rejected explicitly")
    return result


def wikipedia_source_uri(title: str, revid: int) -> str:
    if not isinstance(title, str) or not title.strip() or type(revid) is not int or not 0 < revid <= 2**53 - 1:
        raise NativeAnnotationError("migration requires a title and pinned Wikipedia revision")
    return f"https://en.wikipedia.org/w/index.php?title={quote(title.replace(' ', '_'), safe=_JS_SAFE)}&oldid={revid}"


def repin_items(items: list[dict], *, title: str, old_revid: int, new_revid: int) -> tuple[list[dict], list[dict]]:
    """Move every active article-local source to the new pin, with an audit trail.

    The pin is article metadata, so this is the one change a repin makes to a
    human-owned item; its human attribution stays (the Worker compares human
    items modulo this move). Rejected items keep their historical pins.
    """
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
            source["uri"] = new_uri
            changes.append({"item": item.get("id", index), "occurrence": span_index, "from": previous, "to": new_uri})
    validate_items(result, title=title, revid=new_revid)
    return result, changes
