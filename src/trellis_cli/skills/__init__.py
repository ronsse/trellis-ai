"""Drop-in agent skill templates, shipped as package data.

The canonical copies of the Trellis SKILL.md templates live here so they
travel inside the installed wheel (read via :mod:`importlib.resources`),
not only in a repo checkout. The repo-root ``skills/`` directory is a
pointer to these — see ``skills/README.md``.

Installed into a Claude Code skills directory by
``trellis admin install-skills`` (and ``trellis admin quickstart
--with-skills``). Each subdirectory is a self-contained skill with a
single ``SKILL.md``.
"""

#: Skill directory names shipped in this package, in install order.
SKILL_NAMES: tuple[str, ...] = (
    "retrieve-before-task",
    "record-after-task",
    "link-evidence",
)
