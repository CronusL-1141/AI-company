"""The public format of feedback: the repository's forms, as GitHub renders them.

A bug report filed by OS and one filed through the web form must read the same, so
the body is rendered here exactly as GitHub renders a form submission: one
``### <label>`` section per field, in the form's order, ``_No response_`` for an
empty one, a ``render`` field fenced. The forms themselves live in the repository's
``.github`` folder (``bug_report.yml``, ``DISCUSSION_TEMPLATE/ideas.yml``);
the field ids and labels below are kept equal to them by
tests/unit/feedback/test_forms.py, which reads those files. An installed OS has no
``.github`` folder, so nothing here reads them at run time.

After the last section come a signature line and a hidden marker,
``<!-- aiteam-feedback:v1 form=<form> id=<draft id prefix> h=<content hash prefix> -->``:
the repository's labelling workflow adds the bug label by the form named there, and
the hash prefix finds a post again (reconciling, and dedupe across machines).
"""

from __future__ import annotations

import html
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from aiteam.types import FeedbackKind, FeedbackMode

NO_RESPONSE = "_No response_"
MARKER_PREFIX = "<!-- aiteam-feedback:v1"
SIGNATURE = ("<sub>Filed with the AI Team OS feedback tool: drafted by an AI agent, "
             "reviewed and confirmed by the user.</sub>")
GITHUB = "https://github.com"
# A prefilled page longer than this is not prefilled with the text (GitHub answers 414
# well above it); the text goes into a local file the user copies from.
MAX_PREFILL_URL_BYTES = 6000
HASH_PREFIX = 12
ID_PREFIX = 8


@dataclass(frozen=True)
class FormField:
    id: str
    label: str
    render: str = ""  # a code fence language: GitHub fences the value


@dataclass(frozen=True)
class Form:
    name: str  # also the marker's form=, and the file name without .yml
    path: str  # where the form lives in the repository
    fields: tuple[FormField, ...]


BUG_FORM = Form("bug_report", ".github/ISSUE_TEMPLATE/bug_report.yml", (
    FormField("what-happened", "What happened"),
    FormField("repro", "Steps to reproduce"),
    FormField("version", "AI Team OS version"),
    FormField("install-method", "Install method"),
    FormField("environment", "Environment"),
    FormField("doctor", "/os-doctor output", render="text"),
    FormField("original", "Original wording"),
))
IDEA_FORM = Form("ideas", ".github/DISCUSSION_TEMPLATE/ideas.yml", (
    FormField("problem", "The problem"),
    FormField("proposal", "Proposed solution"),
    FormField("alternatives", "Alternatives considered"),
    FormField("version", "AI Team OS version"),
    FormField("environment", "Environment"),
    FormField("original", "Original wording"),
))
COMMENT_FORM = "comment"  # a comment follows no form

# The values the bug form's "Install method" field names, by aiteam.services.notices.install_kind
# kind. A text field, not a dropdown: GitHub does not prefill a dropdown from the URL, and a
# required dropdown left empty blocks the web submission.
INSTALL_METHODS: dict[str, str] = {
    "cc-plugin": "Claude Code plugin (marketplace)",
    "cc-source": "Claude Code source install (install.py)",
    "codex": "Codex adapter",
    "unknown": "Not sure",
}



def install_method(kind: str) -> str:
    """The dropdown option for an install kind; "Not sure" for one the form does not list."""
    return INSTALL_METHODS.get(kind, INSTALL_METHODS["unknown"])


IDEAS_CATEGORY = "ideas"
QUESTIONS_CATEGORY = "q-a"


@dataclass(frozen=True)
class Diagnostics:
    """The only facts OS adds to a report, every one shown in the preview."""

    version: str
    install_method: str  # a value of INSTALL_METHODS
    host: str  # e.g. "Claude Code 2.1.3"
    platform: str  # e.g. "macOS 15.1, arm64"

    @property
    def environment(self) -> str:
        return ", ".join(part for part in (self.host, self.platform) if part)

    def as_dict(self) -> dict[str, str]:
        return {"version": self.version, "install_method": self.install_method, "host": self.host,
                "platform": self.platform}


_STORED = ("body", "steps", "proposal", "alternatives", "original", "original_language")


@dataclass(frozen=True)
class Draft:
    """The text of one piece of feedback after the checks: what the user wrote, cleaned."""

    kind: FeedbackKind
    mode: FeedbackMode
    title: str
    body: str
    steps: str = ""
    proposal: str = ""
    alternatives: str = ""
    original: str = ""
    original_language: str = ""

    @classmethod
    def of(cls, kind: FeedbackKind, mode: FeedbackMode, title: str, fields: Mapping[str, str]) -> Draft:
        """A draft from the stored (or cleaned) fields; unknown keys are not fields."""
        return cls(kind, mode, title, **{name: fields.get(name, "") for name in _STORED})

    def stored(self) -> dict[str, str]:
        """The fields kept with the row (the title has a column of its own); empty ones left out."""
        values = {name: getattr(self, name) for name in _STORED}
        if not self.original:
            values["original_language"] = ""
        return {name: value for name, value in values.items() if value}


def form_of(draft: Draft) -> Form | None:
    if draft.mode is FeedbackMode.COMMENT:
        return None
    return BUG_FORM if draft.kind is FeedbackKind.BUG else IDEA_FORM


def _original(draft: Draft) -> str:
    if not draft.original.strip():
        return ""
    summary = f"Show the original ({draft.original_language})" if draft.original_language else "Show the original"
    return f"<details><summary>{summary}</summary>\n\n{draft.original.strip()}\n\n</details>"


def field_values(draft: Draft, diagnostics: Diagnostics) -> dict[str, str]:
    """The value of every field of the draft's form (a missing one renders as no response)."""
    common = {"version": diagnostics.version, "environment": diagnostics.environment, "original": _original(draft)}
    if draft.kind is FeedbackKind.BUG:
        return {"what-happened": draft.body, "repro": draft.steps, "install-method": diagnostics.install_method,
                **common}
    return {"problem": draft.body, "proposal": draft.proposal, "alternatives": draft.alternatives, **common}


def render_form(form: Form, values: Mapping[str, str]) -> str:
    """The body GitHub makes of a submission of ``form`` with ``values``.

    An empty field reads ``_No response_``, except a ``render`` field, which GitHub
    fences even when empty (compared byte for byte against a web submission).
    """
    sections = []
    for field in form.fields:
        value = (values.get(field.id) or "").strip()
        if field.render:
            value = f"```{field.render}\n{value}\n```"
        elif not value:
            value = NO_RESPONSE
        sections.append(f"### {field.label}\n\n{value}")
    return "\n\n".join(sections)


def marker(form_name: str, draft_id: str, content_hash: str) -> str:
    return f"{MARKER_PREFIX} form={form_name} id={draft_id[:ID_PREFIX]} h={content_hash[:HASH_PREFIX]} -->"


def hash_token(content_hash: str) -> str:
    """The part of the marker a search finds a post by."""
    return f"h={content_hash[:HASH_PREFIX]}"


def render_body(draft: Draft, diagnostics: Diagnostics, draft_id: str, content_hash: str) -> str:
    """The whole text published: the form's sections (or the comment), the signature, the marker."""
    form = form_of(draft)
    if form is None:
        parts = [draft.body.strip()]
        if draft.original.strip():
            parts.append(_original(draft))
        parts.append(f"<sub>AI Team OS {diagnostics.version}, {diagnostics.environment}</sub>")
        content, form_name = "\n\n".join(parts), COMMENT_FORM
    else:
        content, form_name = render_form(form, field_values(draft, diagnostics)), form.name
    return f"{content}\n\n---\n\n{SIGNATURE}\n\n{marker(form_name, draft_id, content_hash)}"


def destination(kind: FeedbackKind, mode: FeedbackMode, ref: str, repo: str) -> str:
    """Where a draft goes, in words (the preview and the confirmation dialog)."""
    if mode is FeedbackMode.COMMENT:
        target, _, number = ref.partition(":")
        return f"{repo}: a comment on {target} #{number}"
    return f"{repo}: a new Issue (bug)" if kind is FeedbackKind.BUG else f"{repo}: a new Discussion in Ideas"


# ----------------------------------------------------------------------------
# The full-text page (the confirmation dialog links to it: clients fold long messages)
# ----------------------------------------------------------------------------

# Nothing on the page may run or send: no scripts, no forms, no outside requests.
VIEW_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_VIEW_STYLE = ("body{font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:60rem;margin:2rem auto;"
               "padding:0 1rem}dt{font-weight:600;margin-top:.5rem}dd{margin-left:0}"
               "pre{white-space:pre-wrap;word-break:break-word;background:#f6f8fa;padding:1rem;border-radius:6px}")


def view_page(*, destination: str, title: str, content_hash: str, status: str, body: str) -> str:
    """A read-only HTML page of one draft: where, title, hash, then the exact text in ``<pre>``.

    Every value is HTML-escaped; the page has no script, form or button.
    """
    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    title_row = f"<dt>Title / 标题</dt><dd>{esc(title)}</dd>" if title else ""
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\"><meta name=\"robots\" content=\"noindex\">"
        f"<title>AI Team OS feedback {esc(content_hash[:HASH_PREFIX])}</title><style>{_VIEW_STYLE}</style>"
        "</head><body>\n<h1>AI Team OS feedback / 反馈全文</h1>\n"
        "<p>Read only: publishing is decided in the confirmation dialog. / 只读：发不发在确认框里决定。</p>\n"
        f"<dl><dt>To / 发往</dt><dd>{esc(destination)}</dd>{title_row}"
        f"<dt>Hash / 哈希</dt><dd>{esc(content_hash[:HASH_PREFIX])} ({esc(content_hash)})</dd>"
        f"<dt>Status / 状态</dt><dd>{esc(status)}</dd></dl>\n"
        f"<pre>{esc(body)}</pre>\n</body></html>\n"
    )


# ----------------------------------------------------------------------------
# Pages on GitHub
# ----------------------------------------------------------------------------


def repo_url(repo: str) -> str:
    return f"{GITHUB}/{repo}"


def security_url(repo: str) -> str:
    return f"{repo_url(repo)}/security/advisories/new"


def category_url(repo: str, category: str) -> str:
    return f"{repo_url(repo)}/discussions/categories/{category}"


def target_url(repo: str, ref: str) -> str:
    """The page of ``issue:12`` or ``discussion:5``."""
    kind, _, number = ref.partition(":")
    return f"{repo_url(repo)}/{'issues' if kind == 'issue' else 'discussions'}/{number}"


def _url(base: str, params: Mapping[str, str]) -> str:
    return base + "?" + urllib.parse.urlencode(
        {key: value for key, value in params.items() if value}, quote_via=urllib.parse.quote)


def new_post_url(repo: str, draft: Draft, diagnostics: Diagnostics, *, prefill: bool = True) -> str:
    """The new-post page of the draft's form, prefilled field by field (form field ids are
    query parameters); with ``prefill`` False only the form and the title."""
    form = form_of(draft)
    if form is None:
        raise ValueError("a comment has no new-post page")
    values = field_values(draft, diagnostics) if prefill else {}
    if form is BUG_FORM:
        return _url(f"{repo_url(repo)}/issues/new", {"template": f"{form.name}.yml", "title": draft.title, **values})
    return _url(f"{repo_url(repo)}/discussions/new", {"category": IDEAS_CATEGORY, "title": draft.title, **values})


def fits(url: str) -> bool:
    return len(url.encode("utf-8")) <= MAX_PREFILL_URL_BYTES


def copyable_text(draft: Draft, diagnostics: Diagnostics, body: str) -> str:
    """What the user copies when the page cannot be prefilled: each field's value under
    its label (pasted into the field of that name), or the comment as it is."""
    form = form_of(draft)
    if form is None:
        return body + "\n"
    values = field_values(draft, diagnostics)
    lines = [f"Title:\n\n{draft.title}\n"]
    for field in form.fields:
        lines.append(f"{field.label}:\n\n{(values.get(field.id) or '').strip() or '(leave empty)'}\n")
    return "\n".join(lines)
