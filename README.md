# eml2pdf

Convert `.eml` files to PDF by rendering them to a self-contained HTML
page and printing it with headless Chromium (`--print-to-pdf`).
Sibling of [eml2md](https://github.com/mat21mf/eml2md) and uses the same
MIME walking logic (forwarded `message/rfc822` parts are rendered as
nested blocks instead of replacing the sender's own text).

Features:

- Header block (From, To, Cc, Date, Subject), MIME encoded words decoded
- HTML body preferred, `text/plain` fallback rendered with wrapping
- Inline `cid:` images embedded as `data:` URIs
- Attachment list (name, type, size); optional extraction with `-a DIR`
- Forwarded messages rendered recursively
- Scripts, iframes, forms, event handlers stripped; a Content-Security-Policy
  blocks remote content by default (tracking pixels, reproducible output)
- Tolerates unknown/broken charsets
- Batch mode

## Install

```bash
pip install git+https://github.com/mat21mf/eml2pdf
```

Or locally:

```bash
pip install -e .
```

A Chromium-based browser must be available: `chromium`, `chromium-browser`,
`google-chrome[-stable]`, `microsoft-edge[-stable]` or `brave-browser` in
`PATH`, or set explicitly with `--chromium PATH` / `EML2PDF_CHROMIUM`.

On Rocky Linux: `dnf install chromium` (EPEL). On Ubuntu, `chromium-browser`
and `/snap/bin/chromium` are snaps: they work, but a snap has a private
`/tmp`, so eml2pdf detects them and stages its temporary files in
`~/snap/<name>/common/eml2pdf` instead (override with `--tmpdir` or
`EML2PDF_TMPDIR`; for snaps it must be a non-hidden path under `$HOME` or
under `~/snap/<name>/`). The snap prints harmless `update.go`/dbus noise to
stderr. On WSL, where snapd usually does not work, use the Google Chrome
`.deb` or a portable build (e.g. ungoogled-chromium portablelinux tarball).

## Usage

```bash
eml2pdf input.eml                     # produces input.pdf
eml2pdf input.eml output.pdf          # explicit output path (eml2md style)
eml2pdf input.eml -o out/msg.pdf
eml2pdf *.eml -d pdfs/                # batch
eml2pdf input.eml -a attachments/     # also save attachments
eml2pdf input.eml --paper Letter --margin 20mm
eml2pdf input.eml --allow-remote      # load remote images
eml2pdf input.eml --keep-html         # keep intermediate HTML for debugging
eml2pdf input.eml --html-only         # HTML only, no browser needed
```

`--no-sandbox` is passed to Chromium automatically when running as root
(containers); otherwise add it manually if Chromium refuses to start.

## Requirements

Python 3.10+, `beautifulsoup4`, a Chromium-based browser.

## License

MIT
