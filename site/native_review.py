"""Merging one agent review into stored native Literature Links items.

An agent reviews what a selection *says*; it cannot author the bytes that say
where the selection is. So the stored source selections are never round-tripped
through the model: ``review_view`` shows each one as a read-only reference
(``u0``, ``u1``, …), and ``merge_native_review`` carries it forward from the
stored data, promoting it into a link or keeping it unlinked by name. A model
can therefore never silently delete or alter a selection, and explicit location
edits stay with the editor/proposal flow.

``check_native_output`` is the tripwire between that merge and the write: it
supersedes ``native_annotations.preserve_native_items`` for the pipeline and
compares selections by what they select, because ``resolve_locations`` rewrites
a resolved location in place and that is a projection, not an edit.
``native_annotations.py`` itself is byte-frozen: it sits in the Brain's reviewed
git-harvest tool closure (``brain/git_harvest_sources.py`` TOOL_FILES), whose
generation can only be minted on the operator's machine.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

import native_annotations as native

NativeAnnotationError = native.NativeAnnotationError
_SIDES = ("subject", "object")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def is_reviewable(item: Any) -> bool:
    """Whether the reference contract applies: a native item the merge protects."""
    return native.is_native(item) and item.get("provenance") != "human"


def _editing_key(span: dict) -> Any:
    """The WikiLean anchor a resolved end keeps beside its built-in location."""
    extensions = span.get("extensions")
    wikilean = extensions.get("org.wikilean") if isinstance(extensions, dict) else None
    return wikilean.get("anchor") if isinstance(wikilean, dict) else None


def _without_redundant_base(span: dict) -> dict:
    """A selection without a ``base_uri`` that resolves nothing.

    A base only resolves a relative reference; beside an absolute one it says
    nothing, and promoting such a selection into a link drops it (a link
    wrapper carries the base, and an end may not). Neither spelling changes
    what the selection selects, so neither may read as a loss or an edit.
    """
    uri = span.get("uri")
    if "base_uri" in span and isinstance(uri, str) and _SCHEME.match(uri):
        return {key: value for key, value in span.items() if key != "base_uri"}
    return span


def selection_identity(span: dict) -> str:
    """What a source selection selects, however its location happens to be written.

    ``resolve_locations`` projects a WikiLean profile anchor onto a built-in
    Literature Links location and keeps the editing key in the end's
    extensions, so one selection has two spellings and neither is a change of
    meaning. The identity is therefore the editing key when the end has one,
    and the stored location otherwise (an opaque PDF, LaTeX or generic end).
    """
    span = _without_redundant_base(span)
    location = span.get("location")
    anchor = (location.get("anchor") if isinstance(location, dict)
              and location.get("profile") == native.ANCHOR_PROFILE else None)
    if not isinstance(anchor, dict):
        anchor = _editing_key(span)
    body = {"anchor": anchor} if isinstance(anchor, dict) else {"location": location}
    return native.stable_native([span.get("type"), span.get("uri"), span.get("base_uri"), body])


def _comparable(span: dict) -> dict:
    """A stored selection minus what resolving it against the article writes:
    the editing key in its extensions and the profile's resolution outcome."""
    result = {key: value for key, value in _without_redundant_base(span).items() if key != "extensions"}
    location = result.get("location")
    if isinstance(location, dict) and location.get("profile") == native.ANCHOR_PROFILE:
        result["location"] = {key: value for key, value in location.items() if key != "resolution"}
    return result


def _is_resolution(before: dict, after: dict) -> bool:
    """Whether ``after`` is ``before`` with its WikiLean anchor resolved to a
    built-in Literature Links location — a projection, not an edit.

    The exemption is the pipeline's own rewrite and nothing else: the stored
    end must carry a WikiLean profile anchor, the resolved end must keep that
    exact anchor as its editing key, its new location must be one of the two
    shapes ``anchor_resolve.resolve`` emits, and no other byte of the
    selection may have moved.
    """
    location = before.get("location")
    if not (isinstance(location, dict) and location.get("profile") == native.ANCHOR_PROFILE):
        return False
    if _editing_key(after) != location.get("anchor"):
        return False
    resolved = after.get("location")
    if not isinstance(resolved, dict) or set(resolved) not in ({"quote"}, {"css"}):
        return False
    rest = lambda span: {key: value for key, value in _without_redundant_base(span).items()
                         if key not in ("location", "extensions")}
    return rest(before) == rest(after)


def _end_identity(end: dict) -> str:
    """A selection's identity without its carrier base, for comparing an end
    inside a link with the same selection sitting in ``unlinked``."""
    return selection_identity({key: value for key, value in end.items() if key != "base_uri"})


def _slots(item: dict, start: int = 0):
    """``(reference, container, position, end, carrier)`` for every stored source
    selection of one item, in ``occurrences`` order: link subjects, then unlinked.

    The reference is the stable name an agent uses for that selection in one
    review. Numbering continues from ``start``, so the names run across the
    whole article and one name never means two selections.
    ``carrier is end`` exactly for an unlinked selection.
    """
    index = start
    for link in item.get("links", []):
        if not isinstance(link, dict) or not isinstance(link.get("annotation"), dict):
            continue
        subject = link["annotation"].get("subject")
        for position, end in enumerate(subject if isinstance(subject, list) else []):
            if isinstance(end, dict):
                yield f"u{index}", subject, position, end, link
                index += 1
    unlinked = item.get("unlinked")
    for position, span in enumerate(unlinked if isinstance(unlinked, list) else []):
        if isinstance(span, dict):
            yield f"u{index}", unlinked, position, span, span
            index += 1


def _selection_stub(reference: str, end: dict, anchor: dict | None) -> dict:
    stub = {"ref": reference, "type": end.get("type")}
    if isinstance(anchor, dict):
        stub["selects"] = deepcopy(anchor)
    else:  # An opaque end: it is named and carried, never retyped.
        stub["uri"] = end.get("uri")
        stub["opaque"] = True
    return stub


def review_view(items: list[dict]) -> list[dict]:
    """The agent-facing projection of stored review items.

    Every source selection the merge protects is replaced by a read-only
    reference stub — its name, its mode and what it selects — so a review
    never has to round-trip opaque location bytes it cannot author. Human
    items and legacy objects are shown exactly as stored.

    References are numbered continuously across the whole article, so ``u3``
    names one selection and nothing else. A reference an agent writes in the
    wrong review item therefore names a selection the merge can recognise and
    refuse, instead of silently selecting that item's own ``u3``.
    """
    views, index = [], 0
    for item in items:
        view = deepcopy(item)
        if is_reviewable(item):
            slots = list(_slots(view, index))
            for (reference, container, position, end, _carrier), anchor in zip(slots, native.anchors(view)):
                container[position] = _selection_stub(reference, end, anchor)
            index += len(slots)
        views.append(view)
    return views


def _item_identity(item: dict) -> tuple:
    if isinstance(item.get("id"), str):
        return ("id", item["id"])
    return ("selection", native.anchor_sig(item))


def _overlay(old: Any, new: Any) -> Any:
    if isinstance(old, dict) and isinstance(new, dict):
        return {**deepcopy(old), **{key: _overlay(old.get(key), value) for key, value in new.items()}}
    return deepcopy(new)


def _overlay_ends(old: Any, new: Any, carrier: tuple = ()) -> Any:
    # Ends are matched by what they select, so an echoed end keeps the
    # extensions (such as the editing key) and other fields it left out.
    if not isinstance(old, list) or not isinstance(new, list):
        return new
    key = lambda end: native.stable_native([end.get(k) for k in ("type", "uri", "location", *carrier)])
    previous: dict[str, Any] = {}
    for end in old:
        if isinstance(end, dict):
            previous.setdefault(key(end), end)
    return [_overlay(previous.get(key(end)), end) if isinstance(end, dict) else end for end in new]


def _promote(end: dict, carrier: Any, link: dict) -> dict:
    """One referenced selection as the subject end of ``link``.

    An unlinked selection carries its own base; inside a link the wrapper
    carries it. A relative reference may not change base on the way, so the
    move adopts the base when the link has none and is refused when they differ.
    """
    span = deepcopy(end)
    base = span.pop("base_uri", None) if carrier is end else carrier.get("base_uri")
    uri = span.get("uri")
    if isinstance(uri, str) and _SCHEME.match(uri):
        return span
    if link.get("base_uri") is None and base is not None:
        link["base_uri"] = base
    elif link.get("base_uri") != base:
        raise NativeAnnotationError("a promoted selection cannot change its base URI")
    return span


def _item_references(item: dict) -> bool:
    """Whether any selection of a produced item names a stored one."""
    ends = [end for link in item.get("links") or [] if isinstance(link, dict)
            for end in (link.get("annotation") or {}).get("subject") or []]
    return any(isinstance(span, dict) and "ref" in span
               for span in list(item.get("unlinked") or []) + ends)


def _article_slots(protected: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Per-item ``{reference: (end, carrier)}`` maps plus ``{reference: item id}``
    for the whole article, numbered continuously in ``review_view``'s order."""
    maps, owner, index = [], {}, 0
    for stored in protected:
        slots = list(_slots(stored, index))
        maps.append({reference: (end, carrier) for reference, _c, _p, end, carrier in slots})
        for reference, _c, _p, _end, _carrier in slots:
            owner[reference] = stored.get("id")
        index += len(slots)
    return maps, owner


def _unresolvable(reference: str, owner: dict[str, str]) -> NativeAnnotationError:
    """Why a reference names nothing this review item may act on.

    References run across the whole article, so one written in the wrong item
    still names a real selection — and linking that item's own selection of
    the same number instead would be a wrong annotation written in silence.
    """
    if reference in owner:
        return NativeAnnotationError(
            f"source-selection reference {reference!r} belongs to review item "
            f"[{owner[reference]}]; a reference may only be used in the item it was shown in")
    return NativeAnnotationError(f"unknown source-selection reference {reference!r}")


def _merge_item(old: dict, fresh: dict, report: dict,
                slots: dict[str, tuple], owner: dict[str, str]) -> dict:
    """One reviewed item: the agent's judgement over the stored selections."""
    if not native.is_native(fresh):
        raise NativeAnnotationError("agent replaced native data with a legacy object")
    fresh_links = fresh.get("links", [])
    if not isinstance(fresh_links, list) or any(not isinstance(link, dict) for link in fresh_links):
        raise NativeAnnotationError("agent returned malformed native links")
    identities = [link.get("id") for link in fresh_links]
    if any(not isinstance(name, str) or not name for name in identities) or len(set(identities)) != len(identities):
        raise NativeAnnotationError("invalid or duplicate internal link id")
    copies: dict[str, str] = {}
    for reference, (end, _carrier) in slots.items():
        copies.setdefault(_end_identity(end), reference)
    old_links = {link["id"]: link for link in old["links"]}
    # The subject ends a stored link already holds. Naming one in a second link
    # links the same statement twice; it never moves out of the link it is in,
    # so a re-stated subject that leaves one out is an omission, not a deletion.
    held: dict[Any, list] = {}
    for _reference, _container, _position, end, carrier in _slots(old):
        if carrier is not end:
            held.setdefault(carrier.get("id"), []).append(end)
    promoted: set[str] = set()

    def selections(entries: Any, link: dict) -> list:
        """Resolve one subject array: a reference names a stored selection,
        anything else is a selection the agent authored itself."""
        result = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                raise NativeAnnotationError("invalid source selection in a link subject")
            reference = entry.get("ref")
            if reference is None:
                reference = copies.get(_end_identity(entry))
                if reference is None:  # Genuinely new: the agent found this one.
                    report["new_selections"] += 1
                    result.append(deepcopy(entry))
                    continue
            if reference not in slots:
                raise _unresolvable(reference, owner)
            end, carrier = slots[reference]
            if carrier is end:
                promoted.add(reference)
                report["promoted"] += 1
            result.append(_promote(end, carrier, link))
        return result

    merged = _overlay(old, fresh)
    links = []
    for link in fresh_links:
        previous = old_links.get(link.get("id"))
        built = _overlay(previous, link)
        annotation = built.get("annotation")
        if not isinstance(annotation, dict):
            raise NativeAnnotationError("invalid native link wrapper")
        if "subject" in annotation or previous is None:
            if "subject" in annotation and not isinstance(annotation["subject"], list):
                raise NativeAnnotationError("a native link subject must be an array of source selections")
            annotation["subject"] = selections(annotation.get("subject"), built)
            # A subject the agent re-stated keeps every stored end it left out:
            # a selection already on file can only be linked or disputed.
            named = {_end_identity(span) for span in annotation["subject"] if isinstance(span, dict)}
            kept = [deepcopy(end) for end in held.get(link.get("id"), [])
                    if _end_identity(end) not in named]
            if kept:
                annotation["subject"] = kept + annotation["subject"]
                report["carried_selections"] += len(kept)
            if not annotation["subject"]:
                raise NativeAnnotationError("a native link needs a source selection")
        if "object" in annotation:
            before = previous["annotation"].get("object") if isinstance(previous, dict) else None
            annotation["object"] = _overlay_ends(before, annotation["object"])
        links.append(built)
    carried = {link.get("id") for link in links}
    for link in old["links"]:
        # A link the agent did not mention stays exactly as it is: dropping an
        # internal identity is not an authorized deletion operation.
        if link["id"] not in carried:
            links.append(deepcopy(link))
            report["carried_links"] += 1
    merged["links"] = links

    authored = []
    for entry in fresh.get("unlinked", []) if isinstance(fresh.get("unlinked"), list) else []:
        if not isinstance(entry, dict):
            raise NativeAnnotationError("invalid unlinked source selection")
        reference = entry.get("ref")
        if reference is None:
            if _end_identity(entry) not in copies:
                report["new_selections"] += 1
                authored.append(deepcopy(entry))
            continue
        if reference not in slots:
            raise _unresolvable(reference, owner)
        reason = entry.get("reject")
        if isinstance(reason, str) and reason.strip():
            # A disputed selection is kept; removing one is the editor's call.
            report["rejections"].append({"item": old.get("id"), "ref": reference,
                                         "reason": reason.strip()[:300]})
    # Every stored selection the agent did not link stays unlinked, as stored.
    merged["unlinked"] = [deepcopy(end) for reference, (end, carrier) in slots.items()
                          if carrier is end and reference not in promoted] + authored
    return merged


def merge_native_review(existing: list[dict], produced: list[dict]) -> tuple[list[dict], dict]:
    """Merge one agent review into the stored native items.

    Source selections are carried forward from the stored data, never from the
    agent's copy: the agent names a selection (``{"ref": "u3"}``) to link it,
    disputes it with a reason, or says nothing and it stays unlinked exactly as
    stored. The names run across the whole article, so a reference written in
    the wrong review item is refused and reported rather than linking that
    item's own selection of the same number. A review item the agent omits is carried forward unchanged — a
    retraction is an explicit ``rejected`` status — and one it mangles is
    carried forward too, reported rather than failing the whole article.
    Returns the merged items and a report of what was carried.
    """
    if not isinstance(produced, list) or any(not isinstance(item, dict) for item in produced):
        raise NativeAnnotationError("agent annotations must be review objects")
    protected = [item for item in existing if is_reviewable(item)]
    by_id = {_item_identity(item): item for item in protected}
    if len(by_id) != len(protected):
        raise NativeAnnotationError("ambiguous native work items require stable review IDs before agent edits")
    report: dict = {"carried_items": 0, "carried_links": 0, "carried_selections": 0,
                    "promoted": 0, "new_selections": 0, "rejections": [], "skipped_items": []}
    slot_maps, owner = _article_slots(protected)
    slots_by_key = dict(zip(by_id.keys(), slot_maps))
    result, seen = [], set()
    for fresh in produced:
        key = _item_identity(fresh)
        old = by_id.get(key)
        if old is None:
            # The producer may just have assigned an ID to a migrated item
            # that previously had none. Its source signature identifies it.
            key = ("selection", native.anchor_sig(fresh))
            old = by_id.get(key)
        if old is None:
            if _item_references(fresh):
                report["skipped_items"].append({"item": fresh.get("id"),
                                                "reason": "a new review item cannot reference a stored selection"})
                continue
            result.append(deepcopy(fresh))
            continue
        if key in seen:
            report["skipped_items"].append({"item": old.get("id"),
                                            "reason": "a second output item claims one stored item"})
            continue
        seen.add(key)
        try:
            result.append(_merge_item(old, fresh, report, slots_by_key[key], owner))
        except NativeAnnotationError as exc:
            report["skipped_items"].append({"item": old.get("id"), "reason": str(exc)})
            result.append(deepcopy(old))
    for key, old in by_id.items():
        if key not in seen:
            report["carried_items"] += 1
            result.append(deepcopy(old))
    return result, report


def merge_reports(*reports: dict) -> dict:
    """Several merge reports as one, empty when there is nothing to say."""
    total: dict = {}
    for report in reports:
        for key, value in report.items():
            total[key] = (total.get(key, []) + value if isinstance(value, list)
                          else total.get(key, 0) + value)
    return {key: value for key, value in total.items() if value}


def check_native_output(existing: list[dict], produced: list[dict]) -> list[dict]:
    """Check the pipeline's own finished output against the stored native items.

    The agent-facing merge is ``merge_native_review``; this is the tripwire
    between that merge and the write: no stored review item, internal link or
    source selection may have gone missing on the way, and a stored selection
    must arrive as stored — bar the one rewrite that is the pipeline's own,
    resolving a WikiLean anchor against the pinned article.
    This is never used for human editor saves or to convert legacy objects.
    """
    protected = [item for item in existing if is_reviewable(item)]
    by_id = {_item_identity(item): item for item in protected}
    if len(by_id) != len(protected):
        raise NativeAnnotationError("ambiguous native work items require stable review IDs before agent edits")

    result, seen = [], set()
    for fresh in produced:
        key = _item_identity(fresh)
        old = by_id.get(key)
        if old is None:
            key = ("selection", native.anchor_sig(fresh))
            old = by_id.get(key)
        if old is None:
            result.append(deepcopy(fresh))
            continue
        seen.add(key)
        if not native.is_native(fresh):
            raise NativeAnnotationError("agent replaced native data with a legacy object")
        old_links = {link["id"]: link for link in old["links"]}
        fresh_links = fresh.get("links", [])
        if not isinstance(fresh_links, list) or any(not isinstance(link, dict) for link in fresh_links):
            raise NativeAnnotationError("agent returned malformed native links")
        if old_links.keys() - {link.get("id") for link in fresh_links}:
            raise NativeAnnotationError("the output lost a native link; retain it or reject the review item")
        merged = _overlay(old, fresh)
        merged["links"] = [_overlay(old_links.get(link.get("id")), link) for link in fresh_links]
        # A selection promoted from unlinked keeps what its unlinked end carried.
        promoted = [{key: value for key, value in span.items() if key != "base_uri"} for span in old["unlinked"]]
        for link in merged["links"]:
            previous = old_links.get(link.get("id"))
            annotation = link.get("annotation")
            if not isinstance(annotation, dict):
                continue
            for side in _SIDES:
                if side in annotation:
                    before = (previous["annotation"].get(side) if isinstance(previous, dict)
                              else promoted if side == "subject" else None)
                    annotation[side] = _overlay_ends(before, annotation[side])
        merged["unlinked"] = _overlay_ends(old["unlinked"], merged.get("unlinked"), ("base_uri",))
        # A source selection has no identity of its own, inside a link or out
        # of it, so nothing else would notice its loss: a subject the output
        # re-states without one of its stored ends drops that end as quietly
        # as a vanished unlinked span. Explicit location edits belong to the
        # editor/proposal flow; an opaque PDF/LaTeX/generic selection must not
        # disappear or change on the way to the write. Every occurrence of an
        # identity is a candidate, so an added look-alike cannot stand in for
        # the stored bytes it shadows.
        current: dict[str, list] = {}
        for span in native.occurrences(merged):
            current.setdefault(selection_identity(span), []).append(span)
        for old_span in native.occurrences(old):
            spans = current.get(selection_identity(old_span)) or []
            if not spans:
                raise NativeAnnotationError("the output lost a stored source selection; it must stay where it is or ride a link")
            if not any(_comparable(old_span) == _comparable(span) or _is_resolution(old_span, span)
                       for span in spans):
                raise NativeAnnotationError("the output changed a stored source selection; its stored form must ride through")
        result.append(merged)
    if by_id.keys() - seen:
        raise NativeAnnotationError("the output lost a native review item; mark it rejected explicitly")
    return result
