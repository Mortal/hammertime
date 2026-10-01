#!/usr/bin/env python3
"""Single-shot, AI-assisted git commit tool.

Takes a prompt containing `@./path.txt` references, reads those files from the
git index, sends them to the LLM for one round of edits, then stages and
commits the resulting files.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path

from chat_completions import llm_single_edit_round
from hammertime import (
    git_any_staged_changes,
    git_files_with_unstaged_changes,
    git_cat_file,
    git_write_blob,
    git_rev_parse_show_toplevel,
)

# Set this environment variable to emit the LLM's raw output to a text file.
HAMMERTIME_DEBUG = bool(os.environ.get("HAMMERTIME_DEBUG"))

parser = argparse.ArgumentParser()
parser.add_argument("-m", "--message", help="Commit message")
parser.add_argument(
    "--no-default-prompt",
    action="store_true",
    help="Don't append standard boilerplate to prompt",
)
parser.add_argument(
    "prompt",
    help="Prompt for LLM, with embedded @./file.txt markers pointing to files in the repo.",
)


def main() -> None:
    """Parse the prompt, run one LLM edit round, then commit the result."""
    args = parser.parse_args()
    # Refuse to run if the index already has staged changes we might clobber.
    if git_any_staged_changes():
        raise SystemExit("refuse to run when there are staged changes")
    # Maps repo-relative path -> original file bytes (read from the git index).
    files: dict[str, bytes] = {}
    toplevel = git_rev_parse_show_toplevel()
    # Repo-root-relative path of the cwd; used to build cacheinfo paths below.
    basepath = Path(os.getcwd()).relative_to(toplevel)

    # Rewrites each "@path" reference in the prompt to "@/mnt/<path>"
    # and accumulates the referenced file contents into `files`.
    def repl(mo: re.Match) -> str:
        path = mo.group(1)
        if path.startswith("../"):
            raise SystemExit("all prompt paths must be inside the cwd")
        if not path.startswith("./") and not os.path.exists(path):
            # Not an explicit ./ path and no such file exists: leave the
            # "@..." token untouched (it likely wasn't a file reference).
            return mo.group()
        relpath = path.removeprefix("./")
        if relpath not in files:
            # ":./path" reads the file's content from the git index.
            files[relpath] = git_cat_file(f":./{relpath}")
        return f"@{relpath}"

    # Expand @path refs, then append fixed guardrails for the model.
    prompt = re.sub(r"@([^ ]*)", repl, args.prompt)
    if not args.no_default_prompt:
        prompt += (
            "\nDo not read any other files in the project, "
            "and do not try to run any shell commands (python/bash/...). "
            "Apply all your edits in parallel using a single round of tool calls."
        )
    if not files:
        raise SystemExit("no paths mentioned in prompt, use e.g. @./myfile.txt")
    unstaged = git_files_with_unstaged_changes(file_list=list(files))
    if unstaged:
        raise SystemExit("refuse to run when there are unstaged changes")
    # Record each file's index mode so we can preserve it when writing the
    # edited blob back into the index via update-index later.
    modes: dict[str, str] = {}
    for path in files:
        # Get mode of given path in index
        cmdline = ["git", "ls-files", "-zsc", f"./{path}"]
        mode = subprocess.check_output(cmdline).split()[0].decode()
        # Only regular-file and executable modes are expected here.
        assert mode in ("100644", "100755"), mode
        modes[path] = mode
    raw_output, edited_files = llm_single_edit_round(prompt, files)
    if HAMMERTIME_DEBUG:
        with open("git_copilot.txt", "w") as fp:
            fp.write(raw_output)
    # llm_single_edit_round returns the same set of paths as we passed in.
    assert files.keys() == edited_files.keys()
    # Build "<mode>,<blob-sha>,<repo-relative-path>" entries: write each edited
    # blob to the object store and pair it with its mode and path.
    cacheinfos = [
        f"{modes[path]},{git_write_blob(edited_files[path])},{basepath / path}"
        for path in files
        if edited_files[path] != files[path]
    ]
    if not cacheinfos:
        raise SystemExit("no files were edited")
    # Safety net: bail out (printing cacheinfos for debugging) if anything got
    # staged in the meantime that we'd otherwise clobber.
    if git_any_staged_changes():
        print("\n".join(cacheinfos))
        raise SystemExit("refuse to run when there are staged changes")
    # If a path is now "unstaged", someone edited the working tree concurrently.
    unstaged = git_files_with_unstaged_changes(file_list=list(files))
    # Stage the LLM's edits by writing cacheinfo entries into the index.
    for cacheinfo in cacheinfos:
        subprocess.check_call(("git", "update-index", "--cacheinfo", cacheinfo))
    if unstaged:
        # This means someone edited the files while this script was running,
        # in which case we have staged the changes but we won't touch the worktree.
        raise SystemExit(f"simultaneous edits?? {unstaged}")
    # Apply the LLM's edits to the just-staged files.
    subprocess.check_call(("git", "restore", "--", *files))
    # Make a commit with the LLM's edits.
    cmdline = [
        "git",
        "commit",
        "-nm",
        args.message or "Edits with AI",
        "-m",
        "Using git-copilot with the prompt:",
        "-m",
        args.prompt,
    ]
    # Without -m, open $EDITOR so the user can finish the commit message.
    if not args.message:
        cmdline += ["-e"]
    # Here we exit with the exit code of git commit.
    # We don't want a traceback on failure, so we do not use check_call().
    raise SystemExit(subprocess.call(cmdline))


if __name__ == "__main__":
    main()
