"""Safely reorder and hand-edit commits in a git rebase todo list.

See README.md for usage. Designed to be driven by an editor integration
(see vimplugin.py / vimplugin.vim): the todo list is fed on stdin and the
updated list (or a JSON result) is printed on stdout.
"""

import json
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from cliparse import make_cliparser
from hammertime import (
    git_amend_with_commit_msg,
    git_any_staged_changes,
    git_apply_cached_from_git_show,
    git_apply_cached_recount,
    git_commit_with_same_authorship,
    git_files_with_staged_changes,
    git_is_same,
    git_merge_file,
    git_rev_parse,
    git_rev_parse_show_toplevel,
    git_set_head,
    git_set_head_and_staging,
    git_set_staging,
    git_show_commit_message,
    git_show_numstat,
)


subcommand, main = make_cliparser(__doc__, "htime", "htime_")


@subcommand
def htime_open(rebaseline: str) -> None:
    """Print the commit's diff (git show) for hand-editing.

    The hand-edited diff is fed back to the "write" subcommand.
    """
    todo = parse_sequencer_line(rebaseline)
    assert todo is not None
    assert todo.oid
    # The output of this script is shown in a fresh editor buffer,
    # so it is OK to ignore the exit code of git show here,
    # as git show will write a useful error on stderr if it fails.
    subprocess.call(("git", "show", "--full-index", "--stat", "-U3", todo.oid, "--"))


@subcommand
def htime_write(rebaseline: str) -> None:
    """
    Read hand-edited "git show" on stdin and create new commits to replace `rebaseline`.

    The `rebaseline` must be a git rebase todo line, e.g. "pick abcd123 Edit foo.c",
    and the output of this subcommand is a JSON document that can be passed
    to the "update" subcommand to update the full todo list.
    """
    todo = parse_sequencer_line(rebaseline)
    assert todo is not None
    assert todo.oid
    patchlines = sys.stdin.read()
    res = htime_write_inner(patchlines, len(todo.oid))
    print(json.dumps(res))


class TodoEdits(TypedDict):
    """Edits to a rebase todo list, as produced by htime_write_inner.

    replace:   line text that replaces the target line
    justbelow: line to insert just below the target line
    movedown:  line to move down until it no longer conflicts
    """

    replace: NotRequired[str]
    justbelow: NotRequired[str]
    movedown: NotRequired[str]


def parse_git_patch(thepatch: str) -> tuple[str, str]:
    """Extract (commit hash, commit message) from hand-edited "git show" output."""
    if "\r" in thepatch:
        # Every \r must be part of a \r\n line ending; strip to plain LF.
        assert thepatch.count("\r\n") == thepatch.count("\r")
        thepatch = thepatch.replace("\r", "")
    mo = re.match(r"^commit\s+([0-9a-fA-F]+)", thepatch)
    if mo is None:
        raise Exception("input does not look like a git patch")
    commit_hash = mo.group(1)
    # Peel off, in order: the commit/author/date header block, the diff body,
    # and the "---" separator, leaving just the commit message.
    headers, sep, mainmatter = thepatch.partition("\n\n")
    commit_message, sep, rest = mainmatter.partition("\ndiff --git ")
    commit_message, sep, rest = commit_message.partition("\n---\n")
    return commit_hash, textwrap.dedent(commit_message.strip("\n"))


def htime_write_inner(patchlines: str, oidlen: int) -> TodoEdits:
    """Turn a hand-edited patch into replacement todo-list lines.

    Rewinds HEAD to the commit's parent, applies the edited patch to the
    index, and builds up to two "hammer" commits on top of the original:
    one containing the hand-edits (squashed into the original commit via a
    fixup line) and a revert of those edits (a plain pick inserted further
    down the todo list) so that later commits still apply cleanly.
    """
    if git_any_staged_changes():
        raise SystemExit("refuse to run when there are staged changes")
    commit_hash_patch, commit_msg = parse_git_patch(patchlines)
    commit_hash = git_rev_parse(commit_hash_patch)
    assert commit_hash
    head_sha = git_rev_parse("HEAD")
    assert head_sha
    commit_msg_change = (
        commit_msg and git_show_commit_message(commit_hash) != commit_msg
    )
    patchsubject = commit_msg.splitlines()[0] if commit_msg else ""
    toplevel = git_rev_parse_show_toplevel()
    git_set_head_and_staging(f"{commit_hash}^", None)
    try:
        git_apply_cached_recount(patchlines, cwd=toplevel)
        # Edits applied to index. Diff with previous patch to see if anything changed.
        git_set_head(commit_hash)
        edited_files = git_files_with_staged_changes()
        if not commit_msg_change and not edited_files:
            print("No changes")
            return {}
        if commit_msg_change:
            subject = patchsubject
        else:
            subject = (
                f'Changes to "{patchsubject}"'
                if patchsubject
                else f"Changes to {commit_hash[:oidlen]}"
            )
        revertsubject = (
            f'Revert changes to "{patchsubject}"'
            if patchsubject
            else f"Revert changes to {commit_hash[:oidlen]}"
        )
        if not edited_files:
            # Amend existing commit with the updated commit message.
            git_set_head(f"{commit_hash}^")
            git_commit_with_same_authorship(commit_hash)
            git_amend_with_commit_msg(commit_msg)
            newhead = git_rev_parse("HEAD")
            assert newhead
            subject = commit_msg.splitlines()[0]
            res: TodoEdits = {"replace": f"pick {newhead} {subject}".rstrip()}
            print("No hand-edited files, just a commit message update")
            return res
        # Edits applied to index, and HEAD is at commit_hash.
        # Create fixup commit with the user's edits to the patch.
        git_commit_with_same_authorship(commit_hash)
        if commit_msg_change:
            git_amend_with_commit_msg(commit_msg)
        hammer1 = git_rev_parse("HEAD")
        assert hammer1
        if commit_msg_change:
            res = {"justbelow": f"f -C {hammer1} {subject}".rstrip()}
        else:
            res = {"justbelow": f"f {hammer1} {subject}".rstrip()}
        # Second hammer commit: reset the index to the original commit's tree
        # and commit it on top of hammer1, so this commit reverts the hand-edits.
        git_set_staging(commit_hash)
        git_commit_with_same_authorship(commit_hash)
        git_amend_with_commit_msg(revertsubject)
        hammer2 = git_rev_parse("HEAD")
        assert hammer2
        res["movedown"] = f"pick {hammer2} {revertsubject}".rstrip()
        return res
    finally:
        # Restore the HEAD/index we started from, whatever happened above.
        git_set_head_and_staging(head_sha, None)


@subcommand
def htime_update(rebaseline: str, result: str) -> None:
    """Apply a TodoEdits JSON document (from "write") to the todo list on stdin."""
    lines = sys.stdin.read().splitlines()
    try:
        lineno = lines.index(rebaseline)
    except ValueError:
        raise SystemExit("The given --rebaseline was not found in the input")
    if rebaseline in lines[lineno + 1 :]:
        raise SystemExit("The given --rebaseline occurs several times in the input")
    assert result.startswith("{"), repr(result)
    edits: TodoEdits = json.loads(result)
    assert isinstance(edits, dict)
    targetline = parse_sequencer_line(rebaseline)
    assert targetline is not None
    assert targetline.oid
    if "replace" in edits:
        repl = parse_sequencer_line(edits["replace"], targetline)
        assert repl is not None
        assert repl.oid
        lines[lineno : lineno + 1] = [str(repl)]
    ins = lineno + 1
    if "justbelow" in edits:
        jb = parse_sequencer_line(edits["justbelow"], targetline)
        assert jb is not None
        assert jb.oid
        lines[ins:ins] = [str(jb)]
        ins += 1
    if "movedown" in edits:
        moveline = parse_sequencer_line(edits["movedown"], targetline)
        assert moveline is not None
        assert moveline.oid
        targetnumstat = git_show_numstat(moveline.oid)
        movefiles = sorted(ns.path for ns in targetnumstat.numstat)
        # See how far down we can insert edits["movedown"] without causing conflicts.
        # If we can move it all the way down, insert it commented-out
        # (as it's a revert commit that the user likely doesn't care about).
        while True:
            # Try to see if we can move past the next 'pick' line.
            # First, skip over comments/blanks in the todo list.
            line = parse_sequencer_line(lines[ins]) if ins < len(lines) else None
            skip = 0
            while line is None and ins + skip + 1 < len(lines):
                skip += 1
                line = parse_sequencer_line(lines[ins + skip])
            if line is None:
                # We made it to the end, so we insert it commented-out.
                lines[ins:ins] = [f"# {moveline}"]
                break
            # Skip the identified comments/blanks in the todo list.
            ins += skip
            if not line.oid:
                # Dangerous/foreign line that we cannot check conflicts with.
                # Just emit the 'pick' line here.
                lines[ins:ins] = [f"{moveline}"]
                break
            # Check for conflicts with this next todo line.
            conflictfile = move_conflict("down", moveline.oid, line.oid, movefiles)
            if conflictfile is not None:
                # There was a conflict, so we cannot move past this line.
                lines[ins:ins] = [f"{moveline} # {conflictfile}"]
                break
            # No conflict -> move past this line and keep going.
            ins += 1
    print("\n".join(lines))


@dataclass(frozen=True)
class SequencerLine:
    """One parsed git-rebase-todo line, formatted as verb + oid + optional number sign + suffix."""

    verb: str
    oid: str
    # sep: Whitespace and optional number sign between oid and suffix.
    sep: str
    # suffix: Commit subject without leading whitespace.
    suffix: str

    def update(
        self,
        *,
        verb: str | None = None,
        oid: str | None = None,
        suffix: str | None = None,
    ) -> "SequencerLine":
        """Return a copy with the given fields replaced (oid truncated to the existing abbreviation length)."""
        if verb is None:
            verb = self.verb
        else:
            assert verb.endswith(" ")
            if len(self.verb.rstrip()) == 1:
                # self.verb is abbreviated, so also abbreviate the new verb
                verb = {
                    "exec": "x",
                    "pick": "p",
                    "reword": "r",
                    "squash": "s",
                    "fixup": "f",
                    "edit": "e",
                }[verb.rstrip()] + " "
        if oid is None:
            oid = self.oid
        else:
            oid = oid[: len(self.oid)]
        if suffix is None:
            suffix = self.suffix
        else:
            assert suffix.endswith("\n") == self.suffix.endswith("\n")
        return SequencerLine(verb, oid, self.sep, suffix)

    def __str__(self) -> str:
        return f"{self.verb}{self.oid}{self.sep}{self.suffix}"


def parse_sequencer_line(
    line: str, like: SequencerLine | None = None
) -> SequencerLine | None:
    """Parse one git-rebase-todo line.

    Returns None for comments, blank lines, and commands that are safe to
    move commits past (drop/label/break/update-ref). Returns a line with an
    empty oid for commands that are not safe to move past (exec/reset/merge).
    When `like` is given, match its oid abbreviation length and separator.

    >>> assert parse_sequencer_line('t asd') is not None
    >>> assert parse_sequencer_line('pick 1234567') is not None
    """
    # Two alternatives: (1) an oid-bearing command (pick/reword/edit/squash/
    # fixup, long or abbreviated, optional -C flag) captured as
    # verb/oid/sep/suffix; (2) any other command verb with its raw argument.
    mo = re.fullmatch(
        r"^(?:(\s*(?:p|pick|r|reword|e|edit|s|squash|f|fixup)\s*(?:-[Cc]\s*)?)([0-9a-f]+)(\Z|\s+#?\s*)((?:.*)?)|(\s*[a-z][a-z-]*)(.*))\Z",
        line,
        re.S,
    )
    if mo is None:
        return None
    verb, oid, sep, suffix, otherverb, otherarg = mo.groups()
    if otherverb:
        if otherverb.strip() in (
            "d",
            "drop",
            "l",
            "label",
            "b",
            "break",
            "u",
            "update-ref",
        ):
            # These are safe to move past - pretend they don't match
            return None
        # exec, reset, merge -> these are dangerous
        return SequencerLine(otherverb, "", "", otherarg)
    if like:
        oid = oid[: len(like.oid)]
        sep = like.sep
    return SequencerLine(verb, oid, sep, suffix)


def move_conflict(
    up_or_down: Literal["down", "up"], moveoid: str, lineoid: str, movefiles: list[str]
) -> str | None:
    """Check whether `moveoid` can be moved past `lineoid` without changing history.

    Returns None if the move is safe, otherwise a human-readable reason.
    Simulates applying the two commits in the other order (via git
    merge-file on each path they share) and checks the result is identical.
    """
    # Paths touched by the line being moved past.
    touched_paths = {ns.path for ns in git_show_numstat(lineoid).numstat}
    # Paths involved in a merge conflict, making us unable to move the commit.
    conflict_paths: list[str] = []
    if up_or_down == "up":
        error_prefix = f"Cannot move {moveoid} up above {lineoid}"
    else:
        error_prefix = f"Cannot move {moveoid} down below {lineoid}"
    for path in movefiles:
        # Can moveoid's changes to `path` be moved past this line?
        if path not in touched_paths:
            # Trivially yes, since this line doesn't modify `path`
            continue
        if up_or_down == "up":
            # Move B up above A: Try to apply B to A^.
            oid, conflicts = git_merge_file(
                current=f"{lineoid}^:{path}",
                base=f"{moveoid}^:{path}",
                other=f"{moveoid}:{path}",
            )
            if conflicts:
                conflict_paths.append(path)
                continue
            # Then apply A and check that we obtain AB.
            oid, conflicts = git_merge_file(
                current=oid,
                base=f"{lineoid}^:{path}",
                other=f"{lineoid}:{path}",
            )
            if conflicts:
                return f"{errmsg}: Commits cancel out on {path}"
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}:{path}",
                base=f"{moveoid}^:{path}",
                other=f"{moveoid}:{path}",
            )
            if expectedconflicts:
                raise Exception(f"{errmsg}: Initial state has a conflict on {path}")
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return f"{errmsg}: Commits are non-commutative on {path}"
        else:
            # Move A down below B: Try to revert A on B.
            oid, conflicts = git_merge_file(
                current=f"{lineoid}:{path}",
                base=f"{moveoid}:{path}",
                other=f"{moveoid}^:{path}",
            )
            if conflicts:
                conflict_paths.append(path)
                continue
            # Then revert B and check that we obtain the base file.
            oid, conflicts = git_merge_file(
                current=oid,
                base=f"{lineoid}:{path}",
                other=f"{lineoid}^:{path}",
            )
            if conflicts:
                return f"{errmsg}: Commits cancel out on {path}"
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}^:{path}",
                base=f"{moveoid}:{path}",
                other=f"{moveoid}^:{path}",
            )
            if expectedconflicts:
                raise Exception(f"{errmsg}: Initial state has a conflict on {path}")
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return f"{errmsg}: Commits are non-commutative on {path}"
    if conflict_paths:
        return f"{errmsg}: Merge conflict on {', '.join(conflict_paths)}"
    return None


@subcommand
def htime_move(lineno: int, up_or_down: Literal["down", "up"]) -> None:
    """Print JSON describing how far the given (1-indexed) line can move.

    {"movelines": n} if it can move, or {"message": reason} if it cannot.
    """
    lines = sys.stdin.read().splitlines()
    assert 1 <= lineno <= len(lines)
    targetline = parse_sequencer_line(lines[lineno - 1])
    if targetline is None or not targetline.oid:
        print(json.dumps({"message": "Please put the cursor on a 'pick' line"}))
        return
    moveoid = targetline.oid
    movefiles = [ns.path for ns in git_show_numstat(moveoid).numstat]
    dd = 1 if up_or_down == "down" else -1
    ix = lineno - 1 + dd
    # extra: unparseable lines passed since the last parseable line (they
    # must be jumped over together); mv: total line distance travelled.
    extra = 0
    mv = 0
    conflictmessage: str | None = None
    while 0 <= ix < len(lines):
        line = parse_sequencer_line(lines[ix])
        if line is None:
            extra += 1
            ix += dd
            continue
        if not line.oid:
            # exec, reset, merge -> these are dangerous
            conflictmessage = f"Don't want to move past '{line.verb}' command"
            break
        conflictmessage = move_conflict(up_or_down, moveoid, line.oid, movefiles)
        if conflictmessage is not None:
            break
        # This line and any unparseable lines before it all count toward the distance.
        mv += extra + 1
        extra = 0
        ix += dd
    if mv == 0:
        if conflictmessage:
            print(json.dumps({"message": str(conflictmessage)}))
        else:
            print("{}")
    else:
        print(json.dumps({"movelines": mv}))


if __name__ == "__main__":
    main()
