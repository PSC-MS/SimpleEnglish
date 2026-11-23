import json, os, pathlib, subprocess, sys, tempfile
HERE = pathlib.Path(__file__).resolve().parent
HOOK = HERE / "lint_hook.py"
CLEAN = {"CLAUDE_CONFIG_DIR": "/nonexistent/claude-config", "SIMPLE_ENGLISH_LINT_EXCLUDE": ""}
SLOP = "You should simply leverage the robust tool, making it seamless.\n"

def run(event, env=None):
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True,
                       env={**os.environ, **CLEAN, **(env or {})})
    return r.returncode, r.stdout, r.stderr

def warn(event, env=None):
    """Run a PostToolUse event. Return the warning text, or "" when there is none.

    The hook must never block: it exits 0, writes nothing to stderr, and sends a
    warning only as hookSpecificOutput.additionalContext on stdout.
    """
    code, out, err = run(event, env)
    assert code == 0 and err == "", (code, err)
    if not out.strip():
        return ""
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse", output
    return output["additionalContext"]

def write_slop(directory, name="notes.md"):
    path = pathlib.Path(directory, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SLOP, encoding="utf-8")
    return str(path)

def post(path, **event):
    return {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": path}, **event}

def test_post_tool_use_flags_a_slop_markdown_file():
    with tempfile.TemporaryDirectory() as d:
        warning = warn(post(write_slop(d)))
    assert warning and "STE violations" in warning, warning

def test_post_tool_use_prints_the_offending_text_and_line_number():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text("# Title\n\nYou should check this. It has been done.\n", encoding="utf-8")
        warning = warn(post(str(path)))
    assert warning, warning
    assert "line 3, banned_modal: should" in warning, warning
    assert "line 3, perfect_tense: has been" in warning, warning

def test_post_tool_use_ignores_clean_file_and_non_markdown():
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write("Run the migration. Then restart the service.\n"); path = f.name
    assert not warn({"hook_event_name": "PostToolUse", "tool_input": {"file_path": path}})
    assert not warn({"hook_event_name": "PostToolUse", "tool_input": {"file_path": "/tmp/x.py"}})

def test_post_tool_use_skips_the_claude_config_dir():
    with tempfile.TemporaryDirectory() as d:
        path = write_slop(pathlib.Path(d, "projects", "repo", "memory"), "MEMORY.md")
        assert warn(post(path)), "a memory file outside any config dir still lints"
        assert not warn(post(path), env={"CLAUDE_CONFIG_DIR": d})

def test_post_tool_use_skips_a_dot_claude_directory():
    with tempfile.TemporaryDirectory() as d:
        assert not warn(post(write_slop(pathlib.Path(d, ".claude", "agent-memory", "reviewer"), "MEMORY.md")))

def test_post_tool_use_honours_the_exclude_globs():
    with tempfile.TemporaryDirectory() as d:
        path = write_slop(d)
        assert warn(post(path))
        globs = os.pathsep.join(["~/no-such-file.md", f"{d}/*.md"])
        assert not warn(post(path), env={"SIMPLE_ENGLISH_LINT_EXCLUDE": globs})

def test_post_tool_use_resolves_a_relative_path_against_the_event_cwd():
    with tempfile.TemporaryDirectory() as d:
        write_slop(d)
        assert warn(post("notes.md", cwd=d))

def test_post_tool_use_lints_a_path_that_escapes_a_dot_claude_directory():
    with tempfile.TemporaryDirectory() as d:
        write_slop(d)
        pathlib.Path(d, ".claude").mkdir()
        assert warn(post(str(pathlib.Path(d, ".claude", "..", "notes.md"))))

def test_post_tool_use_skips_a_symlink_into_a_dot_claude_directory():
    with tempfile.TemporaryDirectory() as d:
        link = pathlib.Path(d, "notes.md")
        link.symlink_to(write_slop(pathlib.Path(d, ".claude"), "MEMORY.md"))
        assert not warn(post(str(link)))

def test_post_tool_use_skips_a_symlinked_dot_claude_directory():
    with tempfile.TemporaryDirectory() as d:
        write_slop(pathlib.Path(d, "dotfiles"), "MEMORY.md")
        pathlib.Path(d, ".claude").symlink_to(pathlib.Path(d, "dotfiles"))
        assert not warn(post(str(pathlib.Path(d, ".claude", "MEMORY.md"))))

def test_post_tool_use_matches_a_glob_through_a_symlinked_directory():
    with tempfile.TemporaryDirectory() as d:
        write_slop(pathlib.Path(d, "store"))
        pathlib.Path(d, "notes").symlink_to(pathlib.Path(d, "store"))
        path = str(pathlib.Path(d, "notes", "notes.md"))
        assert warn(post(path))
        assert not warn(post(path), env={"SIMPLE_ENGLISH_LINT_EXCLUDE": f"{d}/notes/*"})

def edit(path, new_string, tool_name="Edit", **tool_input):
    return {"hook_event_name": "PostToolUse", "tool_name": tool_name,
            "tool_input": {"file_path": path, "old_string": "x", "new_string": new_string, **tool_input}}

def test_post_tool_use_ignores_old_violations_outside_a_clean_edit():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text(SLOP + "Run the tests.\n", encoding="utf-8")
        warning = warn(edit(str(path), "Run the tests."))
    assert not warning, warning

def test_post_tool_use_reports_only_the_lines_an_edit_wrote():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text(SLOP + "\nIt has been done.\n", encoding="utf-8")
        warning = warn(edit(str(path), "It has been done."))
    assert warning, warning
    assert "line 3, perfect_tense: has been" in warning, warning
    assert "should" not in warning and "line 1" not in warning, warning

def test_post_tool_use_reports_every_place_a_replace_all_wrote():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text("You should run it.\n\nRun the tests.\n\nYou should run it.\n", encoding="utf-8")
        warning = warn(edit(str(path), "You should run it.", replace_all=True))
    assert "line 1, banned_modal" in warning and "line 5, banned_modal" in warning, warning

def test_post_tool_use_checks_each_multiedit_entry():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text("Run the tests.\n\n" + SLOP + "\nYou could restart it.\n", encoding="utf-8")
        event = {"hook_event_name": "PostToolUse", "tool_name": "MultiEdit",
                 "tool_input": {"file_path": str(path), "edits": [
                     {"old_string": "a", "new_string": "Run the tests."},
                     {"old_string": "b", "new_string": "You could restart it."}]}}
        warning = warn(event)
    assert warning and "line 5, banned_modal: could" in warning and "should" not in warning, warning

def test_post_tool_use_passes_a_deletion():
    with tempfile.TemporaryDirectory() as d:
        warning = warn(edit(write_slop(d), ""))
    assert not warning, warning

def test_post_tool_use_counts_lines_after_a_code_fence():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d, "notes.md")
        path.write_text("Run this.\n\n```\nmake\nmake test\n```\n\nYou should stop.\n", encoding="utf-8")
        warning = warn(edit(str(path), "You should stop."))
    assert "line 8, banned_modal: should" in warning, warning

def test_stop_flags_slop_reply_and_never_blocks():
    long = "Great question! " + "This is a robust sentence. " * 7 + "I hope this helps!"
    code, out, err = run({"hook_event_name": "Stop", "last_assistant_message": long})
    assert code == 0 and "systemMessage" in out, (code, out)
    msg = json.loads(out)["systemMessage"]
    assert "opener" in msg and "closer" in msg and "slop" in msg, msg

def test_stop_does_not_count_sentences():
    """The reply register has no sentence cap since 2.1.0. Length alone is not a defect."""
    reply = " ".join(f"The step {i} runs." for i in range(20))
    code, out, err = run({"hook_event_name": "Stop", "last_assistant_message": reply})
    assert code == 0 and out.strip() == "", (code, out)

def test_stop_is_silent_on_a_good_reply():
    code, out, err = run({"hook_event_name": "Stop", "last_assistant_message": "The build failed because the disk was full. Free 2 GB and run it again."})
    assert code == 0 and out.strip() == "", (code, out)

def test_garbage_stdin_exits_zero():
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert r.returncode == 0

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
