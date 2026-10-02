#!/usr/bin/env python3
"""Build system for the LaTeX CV. Standard library only (Python >= 3.11).

    ./build.py                      build every variant in every language
    ./build.py short                one variant, all languages
    ./build.py -l id                all variants, Indonesian only
    ./build.py --watch short        rebuild whenever cv.toml / cv.tex change
    ./build.py --missing            list fields that still lack a translation
    ./build.py --tex-only           only generate build/*-content.tex
    ./build.py clean                remove build/ and dist/

Pipeline (one PDF per variant x language, compiled in parallel):
    cv.toml --build.py--> build/cv-<variant>-<lang>-content.tex
    cv.tex  --latexmk-->  build/cv-<variant>-<lang>.pdf --> dist/<filename><suffix>.pdf

Translations live next to the English text in cv.toml as `<key>_<lang>`
(e.g. `title_id`, `text_id`, `bullets_id`). A missing translation falls back
to the first language defined under [languages].
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "cv.toml"
TEMPLATE = ROOT / "cv.tex"
BUILD = ROOT / "build"
DIST = ROOT / "dist"

DEFAULT_LANG = {
    "suffix": "",
    "babel": "english",
    "present": "Present",
    "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
}
# `--missing` ignores fields shorter than this many words (names, places, ...)
MIN_WORDS_TO_TRANSLATE = 3


# --------------------------------------------------------------------------
# Text -> LaTeX
# --------------------------------------------------------------------------
_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_INLINE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>[^)\s]+)\)"  # [label](url)
    r"|\*\*(?P<bold>.+?)\*\*"                      # **bold**
    r"|\*(?P<ital>[^*]+?)\*"                       # *italic*
    r"|`(?P<code>[^`]+)`"                          # `code`
)


def esc(s: str) -> str:
    return "".join(_SPECIAL.get(c, c) for c in s)


def url_esc(u: str) -> str:
    return u.replace("%", r"\%").replace("#", r"\#").replace("&", r"\&")


def md(s: str) -> str:
    """Tiny inline-markdown -> LaTeX converter (escapes everything else)."""
    out, pos = [], 0
    for m in _INLINE.finditer(s):
        out.append(esc(s[pos:m.start()]))
        if m.group("url"):
            out.append("\\href{%s}{%s}" %
                       (url_esc(m.group("url")), md(m.group("label"))))
        elif m.group("bold"):
            out.append("\\textbf{%s}" % md(m.group("bold")))
        elif m.group("ital"):
            out.append("\\emph{%s}" % md(m.group("ital")))
        else:
            out.append("\\texttt{%s}" % esc(m.group("code")))
        pos = m.end()
    out.append(esc(s[pos:]))
    return "".join(out)


# --------------------------------------------------------------------------
# Language context
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    lang: str                 # language being rendered, e.g. "id"
    default: str              # first language in cv.toml, e.g. "en"
    cfg: dict                 # that language's [languages.<code>] table
    maxp: int                 # variant's max_priority
    missing: list[str] = field(default_factory=list)


def languages(data: dict) -> dict[str, dict]:
    raw = data.get("languages") or {"en": {}}
    out = {}
    for i, (code, cfg) in enumerate(raw.items()):
        suffix = "" if i == 0 else f"-{code}"
        out[code] = {**DEFAULT_LANG, "suffix": suffix, **cfg}
    return out


def tr(ctx: Ctx, obj: dict, key: str, default: str = "") -> str:
    """obj[key] translated: prefers obj['<key>_<lang>'], else falls back to obj[key]."""
    base = obj.get(key, default)
    if ctx.lang == ctx.default:
        return base
    alt = obj.get(f"{key}_{ctx.lang}")
    if alt is not None:
        return alt
    if isinstance(base, str) and len(base.split()) >= MIN_WORDS_TO_TRANSLATE:
        ctx.missing.append(f"{key}_{ctx.lang}: {base[:70]}")
    return base


def fmt_date(value, ctx: Ctx) -> str:
    """'2024-11' -> 'Nov 2024', '2026-09-16' -> '16 Sep 2026', 'present' -> 'Present'."""
    s = str(value).strip()
    if s.lower() == "present":
        return ctx.cfg["present"]
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", s)
    if not m:
        return s
    year, month, day = m.groups()
    parts = []
    if day:
        parts.append(str(int(day)))
    if month:
        parts.append(ctx.cfg["months"][int(month) - 1])
    parts.append(year)
    return " ".join(parts)


def when(item: dict, ctx: Ctx) -> str:
    if "when" in item:                    # free-form override
        return md(tr(ctx, item, "when"))
    if "date" in item:
        return esc(fmt_date(item["date"], ctx))
    start, end = item.get("start"), item.get("end")
    if start and end:
        return "%s~--~%s" % (esc(fmt_date(start, ctx)), esc(fmt_date(end, ctx)))
    return esc(fmt_date(start, ctx)) if start else ""


# --------------------------------------------------------------------------
# Section renderers
# --------------------------------------------------------------------------
def visible(obj: dict, ctx: Ctx) -> bool:
    return obj.get("priority", 1) <= ctx.maxp


def bullets(item: dict, ctx: Ctx) -> list[str]:
    base = [(b, 1) if isinstance(b, str) else (b["text"], b.get("priority", 1))
            for b in item.get("bullets", [])]
    alt = None
    if ctx.lang != ctx.default and base:
        alt = item.get(f"bullets_{ctx.lang}")
        if alt is None:
            ctx.missing.append(f"bullets_{ctx.lang}: {base[0][0][:70]}")
        elif len(alt) != len(base):
            ctx.missing.append(
                f"bullets_{ctx.lang} has {len(alt)} entries but bullets has {len(base)}: {base[0][0][:50]}")
    out = []
    for i, (text, prio) in enumerate(base):
        if prio > ctx.maxp:
            continue
        if alt and i < len(alt):
            text = alt[i] if isinstance(alt[i], str) else alt[i]["text"]
        out.append(text)
    return out


def render_text(sec: dict, ctx: Ctx) -> str:
    return "\\cvtext{%s}" % md(tr(ctx, sec, "text").strip())


def render_entries(sec: dict, ctx: Ctx) -> str:
    out = []
    for it in sec.get("item", []):
        if not visible(it, ctx):
            continue
        title = md(tr(ctx, it, "title"))
        if it.get("url"):
            title = "\\href{%s}{%s}" % (url_esc(it["url"]), title)
        out.append("\\cvhead{%s}{%s}" % (title, when(it, ctx)))
        org, where = tr(ctx, it, "org"), tr(ctx, it, "where")
        if org or where:
            out.append("\\cvsub{%s}{%s}" % (md(org), md(where)))
        if it.get("text"):
            out.append("\\cvtext{%s}" % md(tr(ctx, it, "text")))
        bs = bullets(it, ctx)
        if bs:
            out.append("\\begin{cvbullets}")
            out += ["  \\item %s" % md(b) for b in bs]
            out.append("\\end{cvbullets}")
    return "\n".join(out)


def render_rows(sec: dict, ctx: Ctx) -> str:
    rows = [r for r in sec.get("item", []) if visible(r, ctx)]
    if not rows:
        return ""
    out = ["\\setlength{\\cvdatew}{%s}" % sec.get("label_width", "2.3cm")]
    for r in rows:
        if "label" in r:
            left = "\\textbf{%s}" % md(tr(ctx, r, "label"))
        else:
            left = "\\textcolor{muted}{%s}" % when(r, ctx)
        out.append("\\cvrow{%s}{%s}" % (left, md(tr(ctx, r, "text"))))
    return "\n".join(out)


RENDERERS = {"text": render_text,
             "entries": render_entries, "rows": render_rows}


def generate(data: dict, ctx: Ctx) -> str:
    meta = data["meta"]
    contacts = []
    for c in meta.get("contacts", []):
        t = esc(tr(ctx, c, "text"))
        contacts.append("\\href{%s}{%s}" %
                        (url_esc(c["url"]), t) if c.get("url") else t)
    sep = "\\cvsep "
    out = [
        "% AUTO-GENERATED by build.py from cv.toml. Do not edit.",
        "\\cvlang{%s}" % ctx.cfg["babel"],
        "\\hypersetup{pdfauthor={%s},pdftitle={%s -- CV}}" % (
            meta["name"], meta["name"]),
        "\\cvheader{%s}{%s}{%s}" % (esc(meta["name"]), md(
            tr(ctx, meta, "tagline")), sep.join(contacts)),
    ]
    for sec in data.get("section", []):
        if not sec.get("enabled", True) or not visible(sec, ctx):
            continue
        body = RENDERERS[sec["type"]](sec, ctx)
        if not body.strip():
            continue
        title = tr(ctx, sec, "title")
        if title:
            out.append("\\cvsection{%s}" % esc(title))
        out.append(body)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def load() -> dict:
    with DATA.open("rb") as f:
        return tomllib.load(f)


def tex_errors(job: str, proc: subprocess.CompletedProcess) -> str:
    log = BUILD / f"{job}.log"
    lines = (log.read_text(errors="replace") if log.exists()
             else proc.stdout + proc.stderr).splitlines()
    found = [
        "\n".join(lines[i:i + 4])
        for i, line in enumerate(lines)
        if line.startswith("!") or re.match(r"^\S+\.tex:\d+:", line)
    ]
    return "\n".join(found) if found else "\n".join(lines[-25:])


def page_count(pdf: Path) -> int | None:
    if not shutil.which("pdfinfo"):
        return None
    out = subprocess.run(["pdfinfo", str(pdf)],
                         capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", out)
    return int(m.group(1)) if m else None


def compile_job(job: str, dest_name: str) -> tuple[bool, str]:
    """Run latexmk for one job; returns (ok, message)."""
    proc = subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
         f"-jobname={job}", f"-outdir={BUILD}", TEMPLATE.name],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, "LaTeX failed:\n" + tex_errors(job, proc)
    pdf = BUILD / f"{job}.pdf"
    DIST.mkdir(exist_ok=True)
    dest = DIST / f"{dest_name}.pdf"
    shutil.copyfile(pdf, dest)
    pages = page_count(pdf)
    return True, f"{dest.relative_to(ROOT)}" + (f"  ({pages} page{'s' if pages != 1 else ''})" if pages else "")


def build(variant_sel: list[str], lang_sel: list[str], tex_only: bool) -> bool:
    try:
        data = load()
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"cv.toml error: {e}", file=sys.stderr)
        return False
    variants, langs = data.get("variants", {}), languages(data)
    default = next(iter(langs))
    names, codes = variant_sel or list(variants), lang_sel or list(langs)
    for kind, wanted, have in (("variant", names, variants), ("language", codes, langs)):
        bad = [w for w in wanted if w not in have]
        if bad:
            print(f"unknown {kind}(s): {', '.join(bad)
                                        } (have: {', '.join(have)})", file=sys.stderr)
            return False

    BUILD.mkdir(exist_ok=True)
    jobs = []   # (label, job, dest_name, ctx)
    try:
        for v in names:
            for code in codes:
                ctx = Ctx(code, default, langs[code],
                          variants[v]["max_priority"])
                job = f"cv-{v}-{code}"
                (BUILD / f"{job}-content.tex").write_text(generate(data,
                                                                   ctx), encoding="utf-8")
                dest = variants[v].get(
                    "filename", f"cv-{v}") + langs[code]["suffix"]
                jobs.append((f"{v}/{code}", job, dest, ctx))
    except KeyError as e:
        print(f"cv.toml is missing a required field: {e}", file=sys.stderr)
        return False

    if tex_only:
        for label, job, _, _ in jobs:
            print(f"[{label}] wrote build/{job}-content.tex")
        return True
    if not shutil.which("latexmk"):
        print("latexmk not found. Install TeX Live (see README.md).", file=sys.stderr)
        return False

    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        results = list(pool.map(lambda j: compile_job(j[1], j[2]), jobs))
    ok_all = True
    for (label, _, _, ctx), (ok, msg) in zip(jobs, results):
        print(f"[{label}] {msg}", file=sys.stderr if not ok else sys.stdout)
        if ok and ctx.missing:
            print(f"[{label}]   {len(ctx.missing)
                                 } untranslated field(s), see ./build.py --missing")
        ok_all &= ok
    return ok_all


def report_missing(lang_sel: list[str]) -> bool:
    """Print every translatable field lacking a `_<lang>` counterpart. True if none."""
    data = load()
    langs = languages(data)
    default = next(iter(langs))
    clean = True
    for code in lang_sel or [c for c in langs if c != default]:
        if code == default:
            continue
        ctx = Ctx(code, default, langs[code], maxp=99)
        generate(data, ctx)
        print(f"[{code}] " + (f"{len(ctx.missing)
                                 } untranslated:" if ctx.missing else "complete"))
        for line in dict.fromkeys(ctx.missing):
            print(f"   {line}")
        clean &= not ctx.missing
    return clean


def watch(variant_sel: list[str], lang_sel: list[str]) -> None:
    files = [DATA, TEMPLATE]
    last = None
    print("watching cv.toml and cv.tex (Ctrl-C to stop)")
    try:
        while True:
            stamp = [f.stat().st_mtime for f in files]
            if stamp != last:
                last = stamp
                build(variant_sel, lang_sel, tex_only=False)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the LaTeX CV (all variants x languages) from cv.toml.")
    ap.add_argument("targets", nargs="*",
                    help="variant names, or 'clean' (default: all variants)")
    ap.add_argument("-l", "--lang", action="append", default=[], metavar="CODE",
                    help="language to build, repeatable (default: all in cv.toml)")
    ap.add_argument("-w", "--watch", action="store_true",
                    help="rebuild on every change")
    ap.add_argument("--tex-only", action="store_true",
                    help="generate .tex only, skip LaTeX")
    ap.add_argument("--missing", action="store_true",
                    help="list fields lacking a translation")
    args = ap.parse_args()

    if args.targets == ["clean"]:
        for d in (BUILD, DIST):
            shutil.rmtree(d, ignore_errors=True)
        print("cleaned build/ and dist/")
        return 0
    if args.missing:
        return 0 if report_missing(args.lang) else 1
    if args.watch:
        watch(args.targets, args.lang)
        return 0
    return 0 if build(args.targets, args.lang, args.tex_only) else 1


if __name__ == "__main__":
    sys.exit(main())
