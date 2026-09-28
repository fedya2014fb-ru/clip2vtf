"""Checks the translation files in lang/ against the strings the program actually uses.

    py -3 check_translations.py            report problems (exit code 1 if any)
    py -3 check_translations.py --dump     write lang/_keys.json - every tr() key, for translators

Checks, for every language file:
  - every tr("...") key in clip2vtf.pyw is translated (missing -> falls back to English, then Russian);
  - no stale keys that the program no longer uses;
  - {placeholders} are exactly the same as in the Russian original (a typo there = crash);
  - \\n / \\t count matches (line layout in HELP, tooltips, the drop zone);
  - leading/trailing spaces and punctuation that get glued to other messages are kept;
  - hotkeys (Ctrl+V ...) and technical tokens ($nocull, .bz2, VTF ...) survive translation.
Internal setting values (fit, format, alpha mode...) are translated too: they are shown via tr().
"""
import ast
import json
import os
import re
import string
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "clip2vtf.pyw")
LANG_DIR = os.path.join(HERE, "lang")
# Values stored in settings and shown through tr() at display time (see value_combo).
VALUE_LISTS = ("SIZES", "FITS", "FORMATS", "SHADERS", "ALPHA_MODES", "HAMMER_MODES", "BRUSH_FITS",
               "BRUSH_ALIGNS", "BRUSH_ROTATES", "BRUSH_LUXELS")


def source_keys():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    keys = {}
    consts = {}
    for node in tree.body:  # module-level constants that are shown through tr()
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value
    for name in VALUE_LISTS:
        v = consts.get(name)
        if isinstance(v, ast.Dict):
            items = v.keys
        elif isinstance(v, (ast.List, ast.Tuple)):
            items = v.elts
        else:
            items = []
        for it in items:
            if isinstance(it, ast.Constant) and isinstance(it.value, str):
                keys.setdefault(it.value, it.lineno)
            elif isinstance(it, ast.Name) and isinstance(consts.get(it.id), ast.Constant):
                keys.setdefault(consts[it.id].value, it.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                keys.setdefault(a.value, node.lineno)
            elif isinstance(a, (ast.JoinedStr, ast.BinOp)):  # tr(var.get()) for setting values is fine
                print(f"WARNING line {node.lineno}: tr() of a built string - it can't be translated")
    # only Russian text needs a translation; shader names, "$translucent" etc. stay as they are
    return {k: ln for k, ln in keys.items() if re.search("[а-яА-ЯёЁ]", k)}


def fields(s):
    return sorted(f for _, f, spec, _ in string.Formatter().parse(s) if f is not None)


TOKENS = re.compile(r"Ctrl\+\w+|Shift\+\w+|Alt\+\w+|F1|\$\w+|\.bz2|\.vtf|\.vmt|VTF|VMT|DXT\d|BGRA?8888|"
                    r"Hammer\+\+|infodecal|vtf2tga|materialsrc/|FastDL|TF2|3D|2D|HUD|\d+")


def check(lang, data, keys):
    problems = []
    for k in keys:
        if k not in data or not str(data[k]).strip():
            problems.append(f"missing: {k[:70]!r}")
    for k in data:
        if k not in keys:
            problems.append(f"stale (not used by the program): {k[:70]!r}")
    for k, v in data.items():
        if k not in keys or not v:
            continue
        try:
            if fields(k) != fields(v):
                problems.append(f"placeholders {fields(k)} != {fields(v)}: {v[:70]!r}")
            v.format(**{f.split(".")[0].split("[")[0]: 1 for f in fields(k)}) if fields(k) else None
        except (ValueError, KeyError, IndexError) as e:
            problems.append(f"bad format string ({e}): {v[:70]!r}")
        for ch in ("\n", "\t"):
            if k.count(ch) != v.count(ch):
                problems.append(f"{ch!r} count {k.count(ch)} != {v.count(ch)}: {v[:70]!r}")
        # Chinese/Japanese typography: full-width punctuation, no space before "（"
        starts = {" ": (" ", "（", "，", "。"), ".": (".", "。"), ",": (",", "，")}.get(k[:1], (k[:1],))
        if k[:1] in " .,-(" and not v.startswith(starts):
            problems.append(f"must start with {k[:1]!r} like the original: {v[:70]!r}")
        if k.endswith(" ") != v.endswith(" "):
            problems.append(f"trailing space differs: {v[:70]!r}")
        if k.endswith(("▾", "▴", ":")) and not v.endswith((k[-1], "：" if k[-1] == ":" else k[-1])):
            problems.append(f"must end with {k[-1]!r}: {v[:70]!r}")
        if k.endswith("...") and "..." not in v:  # progress / menu item that opens a dialog
            problems.append(f"lost the '...': {v[:70]!r}")
        want = sorted(t for t in TOKENS.findall(k))
        got = sorted(t for t in TOKENS.findall(v))
        missing = [t for t in set(want) if got.count(t) < want.count(t)]
        if missing:
            problems.append(f"lost {missing}: {v[:70]!r}")
    return problems


def main():
    keys = source_keys()
    if "--dump" in sys.argv:
        with open(os.path.join(LANG_DIR, "_keys.json"), "w", encoding="utf-8") as f:
            json.dump({k: "" for k in sorted(keys, key=keys.get)}, f, ensure_ascii=False, indent=1)
        print(f"{len(keys)} keys -> lang/_keys.json")
        return 0
    bad = 0
    files = sorted(f for f in os.listdir(LANG_DIR) if f.endswith(".json") and not f.startswith("_"))
    for fn in files:
        with open(os.path.join(LANG_DIR, fn), encoding="utf-8") as f:
            data = json.load(f)
        probs = check(fn[:-5], data, keys)
        bad += len(probs)
        print(f"{fn}: {len(data)} strings, {len(probs)} problem(s)")
        for p in probs:
            print("   ", p)
    print(f"{len(keys)} keys in the program, {len(files)} language files")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
