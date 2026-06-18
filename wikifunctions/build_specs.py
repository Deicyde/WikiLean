#!/usr/bin/env python3
"""
build_specs.py — Enrich the addressable (mapped) join rows with each
Wikifunction's real interface, so we can shape a Lean spec for it.

Reads:  ./data/wikifunctions_join.jsonl  (bucket == "mapped" rows; produced by build_join.py)
Writes: ./data/wikifunctions_specs.jsonl

Per function we resolve, straight from the WikiLambda API:
  - English label of the function (Z8 -> Z2K3)
  - arguments: [{key, type_zid, type_label, name}]   (Z8K1 -> Z17s)
  - return_type_zid / return_type_label              (Z8K2)
  - implementations: [{zid, kind}]  kind in {composite, code:<lang>, builtin}
  - n_testers                                        (Z8K3)
These are the facts a spec generator needs: signature + which impls are
composite (provable) vs native (oracle-testable only).
"""
import json, sys, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = "WikiLean-research/1.0 (jack.mccarthy.1@stonybrook.edu)"
API = "https://www.wikifunctions.org/w/api.php"
LANG = {"Z600": "javascript", "Z610": "python", "Z620": "lua"}

_label_cache = {}


def fetch_zids(zids):
    """wikilambda_fetch (one ZID per call) -> {zid: parsed ZObject (the Z2 wrapper)}."""
    out = {}
    for z in dict.fromkeys(zids):
        url = f"{API}?action=wikilambda_fetch&zids={z}&format=json"
        raw = subprocess.run(
            ["curl", "-sS", "--retry", "3", "--max-time", "120",
             "-H", f"User-Agent: {UA}", url],
            capture_output=True, text=True, check=True).stdout
        try:
            d = json.loads(raw)
            out[z] = json.loads(d[z]["wikilambda_fetch"])
        except Exception:
            pass
    return out


def en_label(z2obj):
    """English label from a Z2 wrapper's Z2K3 multilingual string."""
    try:
        for it in z2obj["Z2K3"]["Z12K1"]:
            if isinstance(it, dict) and it.get("Z11K1") == "Z1002":
                return it["Z11K2"]
    except Exception:
        pass
    return None


def label_of(zid, objs):
    if zid in _label_cache:
        return _label_cache[zid]
    lab = en_label(objs[zid]) if zid in objs else None
    _label_cache[zid] = lab or zid
    return _label_cache[zid]


def impl_kind(z14obj):
    """Classify a Z14 implementation: composite / code:<lang> / builtin."""
    body = z14obj.get("Z2K2", z14obj)
    if "Z14K2" in body:
        return "composite"
    if "Z14K3" in body:
        code = body["Z14K3"]
        lang = code.get("Z16K1") if isinstance(code, dict) else None
        return f"code:{LANG.get(lang, lang)}"
    if "Z14K4" in body:
        return "builtin"
    return "unknown"


def arg_name(z17):
    try:
        for it in z17["Z17K3"]["Z12K1"]:
            if isinstance(it, dict) and it.get("Z11K1") == "Z1002":
                return it["Z11K2"]
    except Exception:
        pass
    return None


def main():
    rows = []
    with open(DATA / "wikifunctions_join.jsonl") as f:
        for line in f:
            r = json.loads(line)
            if r["bucket"] == "mapped":
                rows.append(r)

    func_zids = [r["zid"] for r in rows]
    print(f"Fetching {len(func_zids)} function objects...", file=sys.stderr)
    funcs = fetch_zids(func_zids)

    # Gather every referenced impl + type zid, then batch-resolve.
    impl_zids, type_zids = [], []
    for z, obj in funcs.items():
        body = obj.get("Z2K2", {})
        for impl in (body.get("Z8K4") or [])[1:]:   # skip leading "Z14" type tag
            if isinstance(impl, str):
                impl_zids.append(impl)
        for arg in (body.get("Z8K1") or [])[1:]:     # skip leading "Z17" type tag
            if isinstance(arg, dict) and isinstance(arg.get("Z17K1"), str):
                type_zids.append(arg["Z17K1"])
        rt = body.get("Z8K2")
        if isinstance(rt, str):
            type_zids.append(rt)

    print(f"Fetching {len(set(impl_zids))} impls + {len(set(type_zids))} types...",
          file=sys.stderr)
    impls = fetch_zids(impl_zids)
    types = fetch_zids(type_zids)

    out = []
    # A few "functions" linked from Wikidata are actually non-function objects
    # (constants like pi/e, or even a Type). Record what kind each really is.
    extra_types = [obj.get("Z2K2", {}).get("Z1K1") for obj in funcs.values()]
    extra_types = [t for t in extra_types if isinstance(t, str) and t not in ("Z8",)]
    types.update(fetch_zids(extra_types))

    OBJTYPE = {"Z8": "function", "Z4": "type"}
    for r in rows:
        z = r["zid"]
        obj = funcs.get(z, {})
        body = obj.get("Z2K2", {})
        objtype_zid = body.get("Z1K1") if isinstance(body, dict) else None
        objtype = OBJTYPE.get(objtype_zid) or ("constant" if objtype_zid else "unfetched")
        args = []
        for arg in (body.get("Z8K1") or [])[1:]:
            if isinstance(arg, dict):
                t = arg.get("Z17K1")
                args.append({
                    "key": arg.get("Z17K2"),
                    "type_zid": t,
                    "type_label": label_of(t, types) if isinstance(t, str) else None,
                    "name": arg_name(arg),
                })
        rt = body.get("Z8K2")
        impl_list = []
        for impl in (body.get("Z8K4") or [])[1:]:
            if isinstance(impl, str):
                impl_list.append({"zid": impl, "kind": impl_kind(impls.get(impl, {}))})
        testers = [t for t in (body.get("Z8K3") or [])[1:] if isinstance(t, str)]
        out.append({
            "zid": z,
            "qid": r["qid"],
            "wf_object_type": objtype,
            "wf_object_type_label": label_of(objtype_zid, types) if objtype == "constant" else objtype,
            "wf_label": en_label(obj),
            "wikilean_label": r["wikilean_label"],
            "primary_decl": r["primary_decl"],
            "module": r["module"],
            "secondary_decls": r.get("secondary_decls"),
            "decidable_guess": r.get("decidable_guess"),
            "args": args,
            "return_type_zid": rt if isinstance(rt, str) else None,
            "return_type_label": label_of(rt, types) if isinstance(rt, str) else None,
            "implementations": impl_list,
            "n_testers": len(testers),
            "has_composite": any(i["kind"] == "composite" for i in impl_list),
        })

    out.sort(key=lambda r: (r["wf_object_type"] != "function",
                            r["decidable_guess"] != "likely", r["zid"]))
    with open(DATA / "wikifunctions_specs.jsonl", "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")

    funcs_only = [r for r in out if r["wf_object_type"] == "function"]
    print(f"\n{'ZID':>9}  {'signature':<46}  impls / decl")
    print("-" * 110)
    for r in funcs_only:
        sig = "(" + ", ".join(a["type_label"] or "?" for a in r["args"]) + ") -> " + (r["return_type_label"] or "?")
        comp = "★" if r["has_composite"] else " "
        print(f"{r['zid']:>9} {comp} {sig:<46}  {r['primary_decl']}")
    print(f"\nnon-function objects (constants / types) linked from Wikidata:")
    for r in out:
        if r["wf_object_type"] != "function":
            print(f"  {r['zid']:>9}  [{r['wf_object_type_label']}]  {r['primary_decl']}  ({r['wikilean_label']})")
    print(f"\n{len(funcs_only)}/{len(out)} are real Z8 functions; "
          f"{sum(1 for r in funcs_only if r['has_composite'])} have a composite impl (provable layer)")


if __name__ == "__main__":
    main()
