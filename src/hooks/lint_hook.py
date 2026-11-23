#!/usr/bin/env python3
"""Advisory writing checks for Claude Code hooks. Never blocks.

PostToolUse (Write|Edit|MultiEdit on a .md file): lint the file with evals/ste_lint.py
and keep only the hits on the lines that the tool wrote. Old text that the
edit did not touch is not reported. When hits remain, send the offending
text and line number for each hit (capped at MAX_HOOK_HITS) to the model as
a warning: hookSpecificOutput.additionalContext on stdout, with exit 0. An
exit 2 would show the hits as a blocking error, although the tool already
ran. Agent-internal
Markdown, such as memory files under the Claude configuration directory, is
skipped. The writing rules do not govern it, and a summary of its
violations only spends tokens. Set SIMPLE_ENGLISH_LINT_EXCLUDE to skip more
paths.

Stop: read `last_assistant_message`, check the reply register (no bold and
no em-dashes), and return a systemMessage only when the reply
breaks it. Always exit 0, so the session never loops.
"""
import fnmatch
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "evals"))

MAX_HOOK_HITS = 12
CLAUDE_DIR = ".claude"
OPENERS = re.compile(r"^\s*(certainly|great question|you're absolutely right|sure[,!]|absolutely[,!])", re.I)
CLOSERS = re.compile(r"(i hope this helps|let me know if|feel free to)", re.I)


def load_linter():
    try:
        import ste_lint  # noqa: WPS433
        return ste_lint
    except Exception:  # noqa: BLE001
        return None


def strip_code(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`]*`", " ", text)


def absolute(path, cwd=None):
    """Make the path absolute. The harness can send it relative to the session directory."""
    return pathlib.Path(cwd or ".", pathlib.Path(path).expanduser()).absolute()


def variants(path):
    """The path as written and the path with symlinks resolved. An exclusion matches either form.

    A symlink hides a directory name in both directions. `notes.md` can point into `.claude`,
    and `.claude` itself can point at a dotfiles repository. Both forms must miss for the
    file to reach the linter.
    """
    return {pathlib.Path(os.path.normpath(path)), path.resolve()}


def excluded(target):
    """True for agent-internal Markdown and for the paths the user excludes."""
    config_dirs = variants(absolute(os.environ.get("CLAUDE_CONFIG_DIR") or f"~/{CLAUDE_DIR}"))
    raw = os.environ.get("SIMPLE_ENGLISH_LINT_EXCLUDE", "").split(os.pathsep)
    patterns = [os.path.expanduser(p) for p in raw if p]
    for form in variants(target):
        if CLAUDE_DIR in form.parts or any(form.is_relative_to(d) for d in config_dirs):
            return True
        if any(fnmatch.fnmatch(str(form), p) for p in patterns):
            return True
    return False


def written_texts(tool_input):
    """The text that the tool wrote, or None when the payload does not carry it.

    Write carries `content`, Edit carries `new_string`, and MultiEdit carries
    `new_string` in each entry of `edits`.
    """
    if isinstance(tool_input.get("edits"), list):
        return [e.get("new_string") or "" for e in tool_input["edits"]]
    for key in ("new_string", "content"):
        if isinstance(tool_input.get(key), str):
            return [tool_input[key]]
    return None


def written_lines(text, tool_input):
    """The 1-based line numbers in text that hold the text the tool wrote.

    None means "every line": the payload does not say what the tool wrote.
    An Edit with replace_all can write the same text in many places, so every
    occurrence counts. A deletion writes no text and returns an empty set.
    """
    written = written_texts(tool_input)
    if written is None:
        return None
    found = set()
    for fragment in written:
        if not fragment.strip():
            continue
        start = text.find(fragment)
        while start != -1:
            first = text.count("\n", 0, start) + 1
            found.update(range(first, first + fragment.count("\n") + 1))
            start = text.find(fragment, start + len(fragment))
    return found


def post_tool_use(event):
    path = (event.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".md"):
        return 0
    target = absolute(path, event.get("cwd"))
    if excluded(target):
        return 0
    lint = load_linter()
    if lint is None:
        return 0
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return 0
    detail = lint.lint_detail(text, "descriptive")
    lines_written = written_lines(text, event.get("tool_input") or {})
    if lines_written is not None:
        detail = [h for h in detail if h["line"] in lines_written]
    if not detail:
        return 0
    lines = [f"simple-english: the text you wrote in {target.name} has {len(detail)} STE violations."]
    for h in detail[:MAX_HOOK_HITS]:
        lines.append(f"  line {h['line']}, {h['category']}: {h['text']}")
    if len(detail) > MAX_HOOK_HITS:
        lines.append(f"  and {len(detail) - MAX_HOOK_HITS} more hit(s).")
    lines.append("Fix these hits in the text you wrote.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(lines)}}))
    return 0


def stop(event):
    reply = event.get("last_assistant_message") or ""
    problems = []
    lint = load_linter()
    if lint is not None:
        c = lint.reader_check(reply)["counts"]
        for key, label in (("em_dash", "em-dash"), ("bold_spans", "bold span")):
            if c[key]:
                problems.append(f"{c[key]} {label}(s)")
        slop = lint.lint(strip_code(reply), "descriptive")["violations"].get("slop_word", 0)
        if slop:
            problems.append(f"{slop} slop word(s)")
    if OPENERS.search(reply):
        problems.append("a filler opener")
    if CLOSERS.search(reply):
        problems.append("a filler closer")
    if problems:
        print(json.dumps({"systemMessage": "simple-english reply check: " + "; ".join(problems) + "."}))
    return 0


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    try:
        name = event.get("hook_event_name", "")
        if name == "PostToolUse":
            return post_tool_use(event)
        if name == "Stop":
            return stop(event)
    except Exception:  # noqa: BLE001  advisory hook: a crash must never block or loop the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
