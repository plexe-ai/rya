"""The bundled coding-agent skills.

`skills/<name>/SKILL.md` is the copy a human reads in the repo; `SKILLS` in
`src/rya/skills/skill_data.py` is the copy that actually ships (it is in the SDK
wheel, and `rya skills install` writes from it). They are maintained by hand, so
the only thing stopping them diverging is this test — and a diverged pair means
someone edited the documentation while every installed agent kept the old text.
"""

from pathlib import Path

import pytest

from rya.skills import SKILLS

REPO = Path(__file__).resolve().parents[1]
SKILL_DIRS = {"rya": REPO / "skills/rya/SKILL.md",
              "rya-ops": REPO / "skills/rya-ops/SKILL.md"}


def test_every_shipped_skill_has_a_repo_copy():
    assert set(SKILLS) == set(SKILL_DIRS), (
        "add the new skill's SKILL.md path to this test, or drop the stale entry")


@pytest.mark.parametrize("name", sorted(SKILL_DIRS))
def test_the_shipped_skill_matches_the_repo_copy(name):
    on_disk = SKILL_DIRS[name]
    assert on_disk.is_file(), f"{on_disk} is missing"
    assert SKILLS[name].strip() == on_disk.read_text().strip(), (
        f"{on_disk.relative_to(REPO)} and skill_data.py's copy of '{name}' have "
        "diverged. Update both: the file is what a human reads, skill_data.py is "
        "what `rya skills install` writes.")


@pytest.mark.parametrize("name", sorted(SKILL_DIRS))
def test_skill_frontmatter_is_present_and_names_itself(name):
    """`rya skills install` writes these for a coding agent to match on, so a
    missing `name:`/`description:` makes the skill unroutable rather than merely
    untidy."""
    body = SKILLS[name]
    assert body.startswith("---\n"), "frontmatter must open the file"
    _, frontmatter, _ = body.split("---", 2)
    assert f"name: {name}" in frontmatter
    assert "description:" in frontmatter


@pytest.mark.parametrize("name", sorted(SKILL_DIRS))
def test_skill_bodies_survive_the_python_literal(name):
    """They live inside triple-quoted literals in skill_data.py, so a stray
    delimiter or trailing backslash would corrupt the module rather than the text."""
    body = SKILLS[name]
    assert "'''" not in body
    assert not body.rstrip("\n").endswith("\\")
