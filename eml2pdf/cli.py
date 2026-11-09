#!/usr/bin/env python3
"""eml2pdf - convert .eml files to PDF using headless Chromium.

The message is rendered to a self-contained HTML page (headers block,
body, inline cid: images embedded as data: URIs, attachment list,
forwarded messages rendered recursively) and then printed to PDF with
Chromium's --print-to-pdf.

usage:
    eml2pdf input.eml                  # writes input.pdf
    eml2pdf input.eml output.pdf       # explicit output (eml2md style)
    eml2pdf *.eml -d out/              # batch into a directory
"""
import argparse
import base64
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from bs4 import BeautifulSoup

CHROMIUM_CANDIDATES = [
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
]

HEADER_FIELDS = ["From", "To", "Cc", "Date", "Subject"]

# Tags that never make sense in a printed email and may trigger
# network access or active content.
STRIP_TAGS = ["script", "iframe", "object", "embed", "frame", "frameset",
              "base", "link", "form"]

PAGE_CSS = """
@page { size: %(paper)s; margin: %(margin)s; }
html, body { margin: 0; padding: 0; }
body { font-family: "DejaVu Sans", Arial, Helvetica, sans-serif;
       font-size: 11pt; color: #000; background: #fff; }
.eml-headers { border-collapse: collapse; width: 100%%;
               margin-bottom: 10px; font-size: 10pt; }
.eml-headers th { text-align: left; vertical-align: top;
                  padding: 2px 10px 2px 0; white-space: nowrap;
                  color: #444; width: 1%%; }
.eml-headers td { padding: 2px 0; word-break: break-word; }
.eml-subject { font-size: 15pt; font-weight: bold; margin: 0 0 8px 0; }
.eml-sep { border: 0; border-top: 1px solid #999; margin: 8px 0 14px 0; }
.eml-body img { max-width: 100%%; height: auto; }
.eml-body pre, pre.eml-plain { white-space: pre-wrap;
       word-wrap: break-word; overflow-wrap: anywhere;
       font-family: "DejaVu Sans Mono", Consolas, monospace;
       font-size: 9.5pt; margin: 0; }
.eml-attachments { margin-top: 16px; font-size: 9.5pt; }
.eml-attachments ul { margin: 4px 0 0 0; padding-left: 18px; }
.eml-forwarded { margin-top: 18px; padding: 8px 0 0 12px;
                 border-left: 3px solid #bbb; }
.eml-forwarded-title { font-weight: bold; color: #444; margin-bottom: 6px; }
"""

CSP_BLOCK = ("default-src 'none'; img-src data:; style-src 'unsafe-inline' "
             "data:; font-src data:")
CSP_REMOTE = ("default-src 'none'; img-src data: http: https:; "
              "style-src 'unsafe-inline' data: http: https:; "
              "font-src data: http: https:")


# ----------------------------------------------------------------------
# MIME helpers
# ----------------------------------------------------------------------

def safe_content(part):
    """Return decoded text of a text/* part, tolerating bad charsets."""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, AssertionError):
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


def iter_own_parts(msg):
    """Yield leaf parts of msg, without descending into attached
    message/rfc822 parts (those are yielded as-is). Same logic as
    eml2md, so the sender's own text is not replaced by the forwarded
    thread's body."""
    if msg.get_content_type() == "message/rfc822":
        yield msg
        return
    if msg.is_multipart():
        for part in msg.iter_parts():
            yield from iter_own_parts(part)
    else:
        yield msg


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def collect(msg):
    """Split msg into html body, text body, cid map, attachments and
    forwarded messages."""
    body_html, body_text = None, None
    cids = {}          # content-id -> data: URI
    attachments = []   # (filename, content_type, payload bytes)
    forwarded = []
    for part in iter_own_parts(msg):
        ct = part.get_content_type()
        if ct == "message/rfc822":
            forwarded.append(part.get_content())
            continue
        disp = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if ct == "text/html" and disp != "attachment" and body_html is None:
            body_html = safe_content(part)
            continue
        if ct == "text/plain" and disp != "attachment" and not filename \
                and body_text is None:
            body_text = safe_content(part)
            continue
        payload = part.get_payload(decode=True) or b""
        if cid and part.get_content_maintype() == "image":
            b64 = base64.b64encode(payload).decode("ascii")
            cids[cid] = f"data:{ct};base64,{b64}"
        if filename or disp == "attachment":
            attachments.append((filename or "unnamed", ct, payload, cid))
    return body_html, body_text, cids, attachments, forwarded


# ----------------------------------------------------------------------
# HTML building
# ----------------------------------------------------------------------

def sanitize_html(raw, cids, allow_remote=False):
    """Return (styles, body_inner_html, used_cids) from an email HTML
    document."""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()
    for meta in soup.find_all("meta"):
        meta.decompose()
    used = set()
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src.lower().startswith("cid:"):
            key = src[4:].strip("<>")
            if key in cids:
                img["src"] = cids[key]
                used.add(key)
        elif not allow_remote and src.lower().startswith(("http:", "https:", "//")):
            # avoid the broken-image icon; tiny 1x1 images are tracking pixels
            if img.get("width") in ("0", "1") or img.get("height") in ("0", "1"):
                img.decompose()
            else:
                label = img.get("alt") or "remote image"
                img.replace_with(soup.new_string(f"[{label}]"))
    # drop on* event handler attributes
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.lower().startswith("on")]:
            del tag[attr]
    styles = "".join(str(s) for s in soup.find_all("style"))
    for s in soup.find_all("style"):
        s.decompose()
    body = soup.body
    if body is not None:
        wrap_style = body.get("style", "")
        bg = body.get("bgcolor")
        if bg:
            wrap_style = f"background:{bg};" + wrap_style
        inner = body.decode_contents()
        if wrap_style:
            inner = f'<div style="{html.escape(wrap_style)}">{inner}</div>'
    else:
        if soup.head is not None:
            soup.head.decompose()
        inner = str(soup)
    return styles, inner, used


def headers_table(msg):
    rows = []
    for field in HEADER_FIELDS:
        value = msg.get(field)
        if value:
            rows.append(f"<tr><th>{field}:</th>"
                        f"<td>{html.escape(str(value))}</td></tr>")
    return '<table class="eml-headers">' + "".join(rows) + "</table>"


def render_message(msg, allow_remote=False, level=0):
    """Return (styles, html fragment) for one message."""
    body_html, body_text, cids, attachments, forwarded = collect(msg)
    styles = ""
    used = set()
    if body_html:
        styles, inner, used = sanitize_html(body_html, cids, allow_remote)
    elif body_text:
        inner = f'<pre class="eml-plain">{html.escape(body_text)}</pre>'
    else:
        inner = "<p><em>(no body)</em></p>"

    out = []
    subject = msg.get("Subject", "")
    if level == 0:
        out.append(f'<div class="eml-subject">{html.escape(str(subject))}</div>')
    out.append(headers_table(msg))
    out.append('<hr class="eml-sep">')
    out.append(f'<div class="eml-body">{inner}</div>')

    # attachments: skip inline images already shown in the body
    listed = [a for a in attachments if not (a[3] and a[3] in used)]
    if listed:
        items = "".join(
            f"<li>{html.escape(name)} ({html.escape(ct)}, "
            f"{human_size(len(data))})</li>"
            for name, ct, data, _ in listed)
        out.append('<div class="eml-attachments"><strong>Attachments:'
                   f"</strong><ul>{items}</ul></div>")

    for fwd in forwarded:
        fstyles, fhtml = render_message(fwd, allow_remote, level + 1)
        styles += fstyles
        out.append('<div class="eml-forwarded"><div class="eml-forwarded-title">'
                   "Forwarded message</div>" + fhtml + "</div>")
    return styles, "\n".join(out)


def build_page(msg, paper, margin, allow_remote):
    styles, body = render_message(msg, allow_remote)
    csp = CSP_REMOTE if allow_remote else CSP_BLOCK
    title = html.escape(str(msg.get("Subject", "email")))
    css = PAGE_CSS % {"paper": paper, "margin": margin}
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">\n"
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
            f"<title>{title}</title>\n<style>{css}</style>\n{styles}\n"
            f"</head><body>\n{body}\n</body></html>\n")


def extract_attachments(msg, outdir):
    """Write all attachments (including those of forwarded messages)."""
    outdir.mkdir(parents=True, exist_ok=True)
    _, _, _, attachments, forwarded = collect(msg)
    written = []
    for name, _, data, _ in attachments:
        target = outdir / Path(name).name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            target = outdir / f"{stem}_{n}{suffix}"
            n += 1
        target.write_bytes(data)
        written.append(target)
    for fwd in forwarded:
        written += extract_attachments(fwd, outdir)
    return written


# ----------------------------------------------------------------------
# Chromium
# ----------------------------------------------------------------------

def find_chromium(explicit=None):
    for cand in [explicit, os.environ.get("EML2PDF_CHROMIUM")]:
        if cand:
            path = shutil.which(cand) or (cand if Path(cand).is_file() else None)
            if not path:
                sys.exit(f"eml2pdf: chromium binary not found: {cand}")
            return path
    for cand in CHROMIUM_CANDIDATES:
        path = shutil.which(cand)
        if path:
            return path
    sys.exit("eml2pdf: no Chromium/Chrome found in PATH; use --chromium "
             "or set EML2PDF_CHROMIUM")


def snap_name(chrome):
    """Return the snap name if chrome is a snap package, else None.

    Snap apps get a private /tmp and cannot see the caller's /tmp, so
    the HTML input and PDF output must be staged somewhere the snap can
    reach. Detected cases:
      - /snap/bin/<name> (symlink to /usr/bin/snap)
      - Ubuntu transitional wrappers such as /usr/bin/chromium-browser,
        a small shell script that execs /snap/bin/chromium
    """
    path = Path(chrome)
    if path.parent == Path("/snap/bin") or \
            os.path.realpath(chrome) == "/usr/bin/snap":
        return path.name
    try:
        with open(chrome, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    if head.startswith(b"#!"):
        m = re.search(rb"/snap/bin/([A-Za-z0-9][A-Za-z0-9._-]*)", head)
        if m:
            return m.group(1).decode("ascii")
    return None


def work_tmpdir(chrome, explicit=None):
    """Directory where temporary files are created (None = system
    default). Explicit choice wins; for snap browsers use
    ~/snap/<name>/common, which the snap can always read and write."""
    explicit = explicit or os.environ.get("EML2PDF_TMPDIR")
    if explicit:
        Path(explicit).mkdir(parents=True, exist_ok=True)
        return explicit
    name = snap_name(chrome) if chrome else None
    if name:
        d = Path.home() / "snap" / name / "common" / "eml2pdf"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    return None


def print_to_pdf(chrome, html_path, pdf_path, profile_dir, no_sandbox,
                 allow_remote, timeout):
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--hide-scrollbars",
        f"--user-data-dir={profile_dir}",
        "--no-pdf-header-footer",        # current flag name
        "--print-to-pdf-no-header",      # older Chromium versions
        f"--print-to-pdf={pdf_path}",
    ]
    if allow_remote:
        cmd.append("--virtual-time-budget=10000")
    if no_sandbox:
        cmd.append("--no-sandbox")
    cmd.append(Path(html_path).resolve().as_uri())
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"chromium timed out after {timeout}s")
    if not Path(pdf_path).is_file() or Path(pdf_path).stat().st_size == 0:
        raise RuntimeError("chromium did not produce a PDF:\n"
                           + (proc.stderr or proc.stdout).strip()[-2000:])


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def convert(src, dst, chrome, args):
    with open(src, "rb") as f:
        msg = BytesParser(policy=default).parse(f)
    page = build_page(msg, args.paper, args.margin, args.allow_remote)

    if args.html_only:
        dst = dst.with_suffix(".html")
        dst.write_text(page, encoding="utf-8")
        print(f"Written: {dst}")
        return

    if args.extract_attachments:
        adir = Path(args.extract_attachments)
        if len(args.inputs) > 1:
            adir = adir / Path(src).stem
        for p in extract_attachments(msg, adir):
            print(f"Attachment: {p}")

    with tempfile.TemporaryDirectory(prefix="eml2pdf_",
                                     dir=args.tmpdir) as tmp:
        html_path = Path(tmp) / "message.html"
        html_path.write_text(page, encoding="utf-8")
        tmp_pdf = Path(tmp) / "out.pdf"
        print_to_pdf(chrome, html_path, tmp_pdf, Path(tmp) / "profile",
                     args.no_sandbox, args.allow_remote, args.timeout)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_pdf), dst)
        if args.keep_html:
            shutil.copy(html_path, dst.with_suffix(".html"))
    print(f"Written: {dst}")


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="eml2pdf",
        description="Convert .eml files to PDF using headless Chromium.")
    p.add_argument("inputs", nargs="+", metavar="FILE",
                   help="input .eml file(s); with exactly two arguments "
                        "where the second ends in .pdf, it is the output")
    p.add_argument("-o", "--output", help="output PDF (single input only)")
    p.add_argument("-d", "--outdir", help="output directory for batch mode")
    p.add_argument("--chromium", help="Chromium/Chrome binary "
                   "(default: $EML2PDF_CHROMIUM or first found in PATH)")
    p.add_argument("--paper", default="A4",
                   help="CSS @page size, e.g. A4, Letter, 'A4 landscape' "
                        "(default: A4)")
    p.add_argument("--margin", default="15mm",
                   help="CSS @page margin (default: 15mm)")
    p.add_argument("--allow-remote", action="store_true",
                   help="load remote images/styles (blocked by default: "
                        "privacy, tracking pixels, reproducibility)")
    p.add_argument("-a", "--extract-attachments", metavar="DIR",
                   help="also save attachments into DIR "
                        "(DIR/<eml stem>/ in batch mode)")
    p.add_argument("--keep-html", action="store_true",
                   help="keep the intermediate HTML next to the PDF")
    p.add_argument("--html-only", action="store_true",
                   help="only write the intermediate HTML, no Chromium")
    p.add_argument("--no-sandbox", action="store_true",
                   default=(hasattr(os, "geteuid") and os.geteuid() == 0),
                   help="pass --no-sandbox to Chromium "
                        "(automatic when running as root)")
    p.add_argument("--tmpdir",
                   help="directory for temporary files (default: "
                        "$EML2PDF_TMPDIR, ~/snap/<name>/common/eml2pdf for "
                        "snap browsers, else the system temp dir)")
    p.add_argument("--timeout", type=int, default=120,
                   help="seconds per file (default: 120)")
    args = p.parse_args(argv)

    # eml2md compatible form: eml2pdf in.eml out.pdf
    if (len(args.inputs) == 2 and not args.output
            and args.inputs[1].lower().endswith(".pdf")):
        args.output = args.inputs.pop()
    if args.output and len(args.inputs) > 1:
        p.error("--output requires a single input; use --outdir")
    if args.output and args.outdir:
        p.error("--output and --outdir are mutually exclusive")
    return args


def main(argv=None):
    args = parse_args(argv)
    chrome = None if args.html_only else find_chromium(args.chromium)
    args.tmpdir = work_tmpdir(chrome, args.tmpdir)
    failures = 0
    for src in args.inputs:
        src_path = Path(src)
        if args.output:
            dst = Path(args.output)
        elif args.outdir:
            dst = Path(args.outdir) / src_path.with_suffix(".pdf").name
        else:
            dst = src_path.with_suffix(".pdf")
        try:
            convert(src_path, dst, chrome, args)
        except (OSError, RuntimeError) as exc:
            failures += 1
            print(f"eml2pdf: {src}: {exc}", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
