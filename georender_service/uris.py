"""URI helpers shared by sources, assets, and rulesets.

Currently exposes a single function — `expand_github_uri` — which rewrites
`github://<owner>/<repo>[@<ref>]/<path>` to its jsDelivr equivalent. Keeping it
out of `engine.py` and `sources.py` avoids the engine ↔ rules ↔ sources import
cycle that would otherwise appear once rules.py also needs to resolve URIs.
"""
from __future__ import annotations


def expand_github_uri(uri: str) -> str:
    """Expand `github://<owner>/<repo>[@<ref>]/<path>` to a jsDelivr URL.

    Default ref is HEAD (the repo's default branch). The choice of jsDelivr
    matches the geocontext adapter so all assets coming out of the same repo
    end up at predictable URLs the runtime can cache by ref.
    """
    body = uri[len("github://"):]
    if "/" not in body:
        raise ValueError(f"github:// URI missing repo: {uri}")
    owner, rest = body.split("/", 1)
    if "/" not in rest:
        raise ValueError(f"github:// URI missing path: {uri}")
    repo_ref, path = rest.split("/", 1)
    if "@" in repo_ref:
        repo, ref = repo_ref.split("@", 1)
    else:
        repo, ref = repo_ref, "HEAD"
    if not (owner and repo and path):
        raise ValueError(f"github:// URI is malformed: {uri}")
    return f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}"
