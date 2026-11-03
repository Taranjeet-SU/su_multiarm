"""MkDocs build hook: point links that leave ``docs/`` at the repository on GitHub.

The documents under ``docs/`` are written to be read in the repository, so they link to files such
as ``../examples/documents.jsonl``. Those targets are not part of the site; this hook rewrites such
Markdown links to ``<repo_url>/blob/main/<path>`` so the site links work and ``mkdocs build --strict``
still reports every other broken link. Only the docs build loads this file.
"""

from __future__ import annotations

import posixpath
import re

# [text](target) or [text](target "title"); targets starting with ../ only.
LINK = re.compile(r"(\[[^\]]*\]\()(\.\./[^)\s]+)((?:\s+\"[^\"]*\")?\))")


def on_page_markdown(markdown, page, config, files):
    repo = (config.get("repo_url") or "").rstrip("/")
    if not repo:
        return markdown
    page_dir = posixpath.dirname(page.file.src_uri)

    def rewrite(match: re.Match) -> str:
        target = posixpath.normpath(posixpath.join("docs", page_dir, match.group(2)))
        if target.startswith("docs/") or target.startswith(".."):
            return match.group(0)  # inside the docs tree (mkdocs validates it) or outside the repo
        kind = "tree" if target.endswith("/") or "." not in posixpath.basename(target) else "blob"
        return f"{match.group(1)}{repo}/{kind}/main/{target}{match.group(3)}"

    return LINK.sub(rewrite, markdown)
