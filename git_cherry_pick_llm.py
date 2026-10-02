#!/usr/bin/env python3
"""Cherry-pick a commit, resolving file conflicts with an LLM.

Like `git cherry-pick` but instead of dropping conflict markers into the
working tree (or failing), files that don't merge cleanly are handed to an LLM
which re-applies the patch by hand.  The resulting blobs are committed with the
original authorship preserved.
"""

import argparse
import os
import subprocess
from dataclasses import dataclass

from chat_completions import llm_single_edit_round
from hammertime import (
    git_any_staged_changes,
    git_cat_file,
    git_commit_with_same_authorship,
    git_files_with_unstaged_changes,
    git_get_file_mode,
    git_merge_file,
    git_rev_parse_head,
    git_set_head_and_staging,
    git_show,
    git_show_numstat,
    git_write_blob,
)

# Set this environment variable to emit the LLM's raw output to a text file.
HAMMERTIME_DEBUG = bool(os.environ.get("HAMMERTIME_DEBUG"))

parser = argparse.ArgumentParser()
parser.add_argument(
    "--onto",
    help="Instead of cherry-picking on top of HEAD, create a commit object that cherry-picks on top of this commit instead.",
)
parser.add_argument("refspec", help="The commit to be cherry-picked.")


@dataclass(frozen=True)
class AgentWork:
    path: str  # path to be edited
    mode: str  # 100644 or 100755
    the_target: bytes  # contents of file to be edited
    the_patch: bytes  # contents of patch to be applied


PROMPT = (
    "The patch in @/mnt/patch was made on an old version of the file, "
    "and it now fails to apply cleanly to @/mnt/input/{basename} because of recent unrelated changes. "
    "Apply the patch by hand. "
    "If the patch, or part of the patch, cannot be applied because the recent changes make it impossible, "
    "add comments starting with `REVIEW:` explaining what needs to be done. "
    "Do not read any other files in the project, "
    "and do not try to run any shell commands (python/bash/...). "
    "Apply all your edits in parallel using a single round of tool calls."
)


def main() -> None:
    """Perform an LLM-assisted cherry-pick.

    High-level flow:
      1. List every file the picked commit touches.
      2. For each file, attempt a normal cherry-pick;
         files that do not merge cleanly are queued for the LLM to resolve.
      3. Run the LLM one-by-one on each file that did not merge cleanly.
      4. Build a single commit containing all resolved files.

    Without --onto, applies the patch on top of HEAD as a new commit;
    with --onto, applies the patch on top of a different commit
    (without actually picking the commit).
    """
    args = parser.parse_args()
    paths = [entry.path for entry in git_show_numstat(args.refspec).numstat]
    onto = args.onto or git_rev_parse_head()
    # Merged blobs, to be passed to "git update-index --cacheinfo".
    cacheinfos: list[str] = []
    agent_work: list[tuple[str, str, bytes, bytes]] = []
    for path in paths:
        # Try to apply args.refspec's changes on `path` on top of `onto`.
        # Standard cherry-pick three-way merge: `base` is the parent of the
        # picked commit, `other` is the picked commit itself, `current` is the
        # version already present at `onto`.
        oid, conflicts = git_merge_file(
            current=f"{onto}:{path}",
            base=f"{args.refspec}^:{path}",
            other=f"{args.refspec}:{path}",
        )
        # Get mode of given path in index
        mode = git_get_file_mode(path)
        # Remember what to feed "git update-index --cacheinfo" later.
        if not conflicts:
            cacheinfos.append(f"{mode},{oid},{path}")
            continue
        the_patch = git_show(args.refspec, file_list=[path])
        the_target = git_cat_file(f"{onto}:{path}")
        agent_work.append(AgentWork(path, mode, the_target, the_patch))
    if agent_work:
        print(f"Running {len(agent_work)} file(s) through an LLM...")
    # We don't want to clobber any already-staged changes,
    # so we check for staged changes before staging LLM's edits;
    # since running the LLM is slow and expensive we also do an additional check
    # before actually running the LLM.
    if git_any_staged_changes():
        raise SystemExit("refuse to run when there are staged changes")
    # When cherry-picking onto HEAD, also ensure that there are no
    # unstaged changes to the files we are going to edit.
    if args.onto is None and git_files_with_unstaged_changes(file_list=paths):
        raise SystemExit("refuse to run when there are unstaged changes")
    for w in agent_work:
        basename = os.path.basename(w.path)
        raw_output, files = llm_single_edit_round(
            PROMPT.format(basename=basename),
            {"/mnt/patch": w.the_patch, f"/mnt/input/{basename}": w.the_target},
        )
        if HAMMERTIME_DEBUG:
            with open("git_cherry_pick_llm.txt", "w") as ofp:
                ofp.write(raw_output)
        oid = git_write_blob(files[f"/mnt/input/{basename}"])
        cacheinfos.append(f"{w.mode},{oid},{w.path}")
    # Don't clobber any changes staged by other processes.
    if git_any_staged_changes():
        # Print the cacheinfos so that the user can apply them by hand.
        print("Would update the index with git update-index --cacheinfo:")
        print("\n".join(cacheinfos))
        raise SystemExit("refuse to run when there are staged changes")
    if args.onto is None:
        # If a path is now "unstaged", someone edited the working tree concurrently.
        unstaged = git_files_with_unstaged_changes(file_list=paths)
        # Stage the LLM's edits by writing cacheinfo entries into the index.
        for cacheinfo in cacheinfos:
            subprocess.check_call(("git", "update-index", "--cacheinfo", cacheinfo))
        if unstaged:
            # This means someone edited the files while this script was running,
            # in which case we have staged the changes but we won't touch the worktree.
            raise SystemExit(f"simultaneous edits?? {unstaged}")
        # Apply the LLM's edits to the just-staged files.
        subprocess.check_call(("git", "restore", "--", *paths))
        git_commit_with_same_authorship(args.refspec)
        print("DONE")
    else:
        # Detach HEAD at `onto` to build the commit there, then restore the
        # original HEAD in `finally` so the index/worktree are left untouched on
        # both success and failure.
        head_sha = git_rev_parse_head()
        git_set_head_and_staging(onto, None)
        try:
            for cacheinfo in cacheinfos:
                subprocess.check_call(("git", "update-index", "--cacheinfo", cacheinfo))
            git_commit_with_same_authorship(args.refspec)
            result = git_rev_parse_head()
        finally:
            git_set_head_and_staging(head_sha, None)
        # Cherry-pick with --onto was successful - print the commit but don't actually apply it.
        print("Successfully cherry-picked to produce a new commit with id:")
        print(result)


if __name__ == "__main__":
    main()
