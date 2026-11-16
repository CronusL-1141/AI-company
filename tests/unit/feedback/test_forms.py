"""The repository's public forms and what the feedback tool writes stay one format.

A report filed by OS must read like one filed through the web form: the same
sections in the same order, under the same headings. The forms are read from
``.github`` here; the code has its own copy of their ids and labels (an installed
OS has no ``.github`` folder), and this file is what keeps the two equal. The
labelling workflow must recognise exactly the marker the tool writes for a bug.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from aiteam.feedback import forms
from aiteam.types import FeedbackKind, FeedbackMode

ROOT = Path(__file__).resolve().parents[3]
GITHUB = ROOT / ".github"
DIAGNOSTICS = forms.Diagnostics(version="1.16.0", install_method=forms.INSTALL_METHODS["cc-source"],
                                host="Claude Code 2.1.325", platform="macOS 15.1, arm64")


def _fields(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load(path.read_text(encoding="utf-8"))["body"] if item["type"] != "markdown"]


@pytest.mark.parametrize("form", [forms.BUG_FORM, forms.IDEA_FORM], ids=["bug_report", "ideas"])
def test_the_code_knows_each_form_field_by_id_label_and_rendering(form: forms.Form) -> None:
    declared = [(item["id"], item["attributes"]["label"], item["attributes"].get("render", ""))
                for item in _fields(ROOT / form.path)]
    assert declared == [(field.id, field.label, field.render) for field in form.fields]
    assert Path(form.path).stem == form.name


def test_the_install_method_is_a_text_field_naming_every_value_the_tool_writes() -> None:
    # A dropdown is not prefilled from the URL, and left empty it blocks a required field.
    assert not [item for item in _fields(ROOT / forms.BUG_FORM.path) if item["type"] == "dropdown"]
    (field,) = [item for item in _fields(ROOT / forms.BUG_FORM.path) if item["id"] == "install-method"]
    assert field["type"] == "input" and field["validations"]["required"] is True
    description = field["attributes"]["description"]
    assert all(value in description for value in forms.INSTALL_METHODS.values())
    assert field["attributes"]["placeholder"] in forms.INSTALL_METHODS.values()


@pytest.mark.parametrize(("kind", "form"), [(FeedbackKind.BUG, forms.BUG_FORM), (FeedbackKind.IDEA, forms.IDEA_FORM)],
                         ids=["bug", "idea"])
def test_a_rendered_body_has_the_forms_sections_in_order(kind: FeedbackKind, form: forms.Form) -> None:
    draft = forms.Draft(kind, FeedbackMode.NEW, title="t", body="what", steps="1. a", proposal="p")
    body = forms.render_body(draft, DIAGNOSTICS, "d" * 36, "f" * 64)
    headings = re.findall(r"^### (.+)$", body, re.MULTILINE)
    assert headings == [item["attributes"]["label"] for item in _fields(ROOT / form.path)]
    assert forms.NO_RESPONSE in body  # an empty field reads as GitHub renders it


def test_the_labelling_workflow_recognises_the_bug_marker_and_nothing_else() -> None:
    workflow = yaml.safe_load((GITHUB / "workflows" / "feedback-labels.yml").read_text(encoding="utf-8"))
    condition = workflow["jobs"]["label-bug-report"]["if"]
    needle = re.fullmatch(r"contains\(github\.event\.issue\.body, '(.+)'\)", condition).group(1)
    bug = forms.marker(forms.BUG_FORM.name, "d" * 36, "f" * 64)
    assert needle in bug
    assert needle not in forms.marker(forms.IDEA_FORM.name, "d" * 36, "f" * 64)
    assert needle not in forms.marker(forms.COMMENT_FORM, "d" * 36, "f" * 64)
    run = workflow["jobs"]["label-bug-report"]["steps"][0]["run"]
    assert "${{" not in run  # the issue text never reaches the shell


def test_the_issue_chooser_points_ideas_and_security_elsewhere() -> None:
    config = yaml.safe_load((GITHUB / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    urls = [link["url"] for link in config["contact_links"]]
    assert any(url.endswith("/discussions/categories/ideas") for url in urls)
    assert any(url.endswith("/security/advisories/new") for url in urls)


def test_the_public_templates_are_english_without_em_dashes() -> None:
    files = sorted([*(GITHUB / "ISSUE_TEMPLATE").glob("*.yml"), *(GITHUB / "DISCUSSION_TEMPLATE").glob("*.yml"),
                    GITHUB / "PULL_REQUEST_TEMPLATE.md", GITHUB / "workflows" / "feedback-labels.yml"])
    assert len(files) == 7
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "\u2014" not in text, path
        assert not re.search(r"[぀-ヿ㐀-鿿]", text), path


def test_a_prefilled_page_uses_the_forms_field_ids() -> None:
    draft = forms.Draft(FeedbackKind.BUG, FeedbackMode.NEW, title="t", body="what", steps="1. a")
    url = forms.new_post_url("acme/os", draft, DIAGNOSTICS)
    ids = {field.id for field in forms.BUG_FORM.fields}
    query = url.split("?", 1)[1]
    params = {part.split("=", 1)[0] for part in query.split("&")}
    assert params - {"template", "title"} <= ids and "labels" not in params


def test_empty_fields_render_as_a_web_submission_does() -> None:
    """issues/4 of the test repository, a web submission, compared byte for byte with the tool's body:
    an empty field is ``_No response_``, but an empty ``render: text`` field is an empty fence."""
    body = forms.render_form(forms.BUG_FORM, {"what-happened": "x", "repro": "y"})
    assert "### /os-doctor output\n\n```text\n\n```" in body
    assert "### Original wording\n\n_No response_" in body
    filled = forms.render_form(forms.BUG_FORM, {"doctor": "OK"})
    assert "### /os-doctor output\n\n```text\nOK\n```" in filled
