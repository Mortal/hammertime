"""Safely reorder and hand-edit commits in a git rebase todo list.

See README.md for usage. Designed to be driven by an editor integration
(see vimplugin.py / vimplugin.vim): the todo list is fed on stdin and the
updated list (or a JSON result) is printed on stdout.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import Iterator, Literal, NotRequired, TypedDict

from cliparse import make_cliparser
from hammertime import (
    git_amend_with_commit_msg,
    git_any_staged_changes,
    git_apply_cached_from_git_show,
    git_apply_cached_unidiff_zero_from_str,
    git_apply_cached_recount,
    git_commit_with_same_authorship,
    git_files_with_staged_changes,
    git_is_same,
    git_merge_file,
    git_rev_parse,
    git_rev_parse_head,
    git_rev_parse_show_toplevel,
    git_set_head,
    git_set_head_and_staging,
    git_set_staging,
    git_show_commit_message,
    git_show_commit_subject,
    git_show_numstat,
    git_write_tree,
)

HTIME_DEBUG = bool(os.environ.get("HTIME_DEBUG"))

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


@dataclass(frozen=True)
class MoveConflict:
    """Why a commit cannot (yet) be moved past another todo line."""

    error_prefix: str
    error: str
    paths: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.error_prefix}: {self.error} on {', '.join(self.paths)}"


# Special value for MoveConflict.error, matched inside htime_swap().
MERGE_CONFLICT_ERROR = "Merge conflict"


def move_conflict(
    up_or_down: Literal["down", "up"], moveoid: str, lineoid: str, movefiles: list[str]
) -> MoveConflict | None:
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
                return MoveConflict(error_prefix, "Commits cancel out", (path,))
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}:{path}",
                base=f"{moveoid}^:{path}",
                other=f"{moveoid}:{path}",
            )
            if expectedconflicts:
                return MoveConflict(
                    error_prefix, "Initial state has a conflict", (path,)
                )
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return MoveConflict(
                    error_prefix, "Commits are non-commutative", (path,)
                )
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
                return MoveConflict(error_prefix, "Commits cancel out", (path,))
            expected, expectedconflicts = git_merge_file(
                current=f"{lineoid}^:{path}",
                base=f"{moveoid}:{path}",
                other=f"{moveoid}^:{path}",
            )
            if expectedconflicts:
                return MoveConflict(
                    error_prefix, "Initial state has a conflict", (path,)
                )
            if expected != oid:
                # This means the patches can be applied in either order,
                # but the result differs depending on the order.
                return MoveConflict(
                    error_prefix, "Commits are non-commutative", (path,)
                )
    if conflict_paths:
        return MoveConflict(error_prefix, MERGE_CONFLICT_ERROR, tuple(conflict_paths))
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
    conflictmessage: MoveConflict | str | None = None
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


@dataclass(frozen=True)
class DiffLine:
    """One line of a unified diff plus its 0-based old/new line numbers.

    For '+' lines, fromline is the gap position in the old file where the
    line is inserted; for '-' lines, toline is the gap position in the new
    file where the removed line would have been.
    """

    diffline: str
    fromline: int
    toline: int


def diff_parser(lines: Iterator[str]) -> Iterator[DiffLine]:
    """Yield a DiffLine for every hunk-body line of a single-file unified diff.

    The input must start with the five header lines ("diff --git", "index",
    "--- a/", "+++ b/") followed by one or more hunks starting with "@@ ".
    """
    it = iter(lines)
    # TODO: At the moment we don't support mode changes, renames,
    # similarity-index, or binary-diff lines.
    diffline: str | None = next(it, None)
    assert diffline is not None and diffline.startswith("diff --git ")
    diffline = next(it, None)
    assert diffline is not None and diffline.startswith("index ")
    diffline = next(it, None)
    assert diffline is not None and diffline.startswith("--- a/")
    diffline = next(it, None)
    assert diffline is not None and diffline.startswith("+++ b/")
    diffline = next(it, None)
    assert diffline is not None and diffline.startswith("@@ ")
    while diffline is not None:
        mo = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", diffline)
        if mo is None:
            raise Exception(diffline)
        fromline_str, fromcount_str, toline_str, tocount_str = mo.groups()
        fromline = int(fromline_str)
        # Adjust from 1-based line numbering to 0-based line numbering
        fromline -= 1
        fromcount = int(fromcount_str or "1")
        if fromcount == 0:
            # Zero-length ranges point correctly in between lines
            fromline += 1
        toline = int(toline_str)
        # Adjust from 1-based line numbering to 0-based line numbering
        toline -= 1
        tocount = int(tocount_str or "1")
        if tocount == 0:
            # Zero-length ranges point correctly in between lines
            toline += 1
        fromseen = 0
        toseen = 0
        diffline = next(it, None)
        while fromseen < fromcount or toseen < tocount:
            assert diffline is not None
            yield DiffLine(diffline, fromline + fromseen, toline + toseen)
            if diffline.startswith("+"):
                toseen += 1
            elif diffline.startswith("-"):
                fromseen += 1
            elif diffline.startswith(" "):
                fromseen += 1
                toseen += 1
            else:
                raise Exception(diffline)
            diffline = next(it, None)


@dataclass(frozen=True, kw_only=True)
class Edit:
    # old[i1:i2] corresponds to new[j1:j2]
    i1: int
    i2: int
    j1: int
    j2: int
    fromlines: tuple[str, ...] | None = None
    tolines: tuple[str, ...] | None = None

    @property
    def eof(self) -> bool:
        """True for the zero-length end-of-diff sentinel Edit."""
        if self.i1 == self.i2 and self.j1 == self.j2:
            assert self.fromlines is None
            assert self.tolines is None
            return True
        return False

    @property
    def equal(self) -> bool:
        """True for an unchanged (context) range; no +/- lines attached."""
        if self.fromlines is None:
            assert self.tolines is None
            return True
        assert self.tolines is not None
        return False

    def offset(self, offi: int, offj: int) -> "Edit":
        """Return a copy with the old/new ranges shifted by the given amounts."""
        result = Edit(
            i1=self.i1 + offi,
            i2=self.i2 + offi,
            j1=self.j1 + offj,
            j2=self.j2 + offj,
            fromlines=self.fromlines,
            tolines=self.tolines,
        )
        return result

    def net_added(self) -> int:
        """Line-count shift this edit introduces (positive = new side is longer)."""
        return self.j2 - self.j1 - (self.i2 - self.i1)

    def range_str(self) -> str:
        i1 = self.i1
        i2 = self.i2
        j1 = self.j1
        j2 = self.j2
        # Convert back to 1-based line numbering
        i1 += 1
        i2 += 1
        j1 += 1
        j2 += 1
        if self.eof:
            return f"-{i1},EOF +{j1},EOF"
        # Convert back to weird zero-length range convention
        if i1 == i2:
            i1 -= 1
            i2 -= 1
        if j1 == j2:
            j1 -= 1
            j2 -= 1
        if self.equal:
            return f"-{i1},{i2 - i1}==+{j1},{j2 - j1}"
        return f"-{i1},{i2 - i1} +{j1},{j2 - j1}"

    def patchlines(self) -> str:
        if self.fromlines is None or self.tolines is None:
            return ""
        fromlines = "".join(f"-{line}" for line in self.fromlines)
        tolines = "".join(f"+{line}" for line in self.tolines)
        return f"@@ {self.range_str()} @@\n{fromlines}{tolines}"


def opcodes_from_difflines(difflines: Iterator[DiffLine]) -> Iterator[Edit]:
    """Group a DiffLine stream into difflib-opcodes-style Edit records.

    Yields one Edit per contiguous equal/change run, ending with a
    zero-length "eof" Edit marking the end position.
    """
    it = iter(difflines)
    diffline: DiffLine | None = next(it, None)
    if diffline is None:
        raise Exception("opcodes_from_difflines got an empty input")
    first = True
    while diffline is not None:
        if diffline.diffline.startswith(" "):
            if first:
                i1 = j1 = 0
            else:
                i1 = diffline.fromline
                j1 = diffline.toline
            first = False
            i2 = diffline.fromline + 1
            j2 = diffline.toline + 1
            diffline = next(it, None)
            while diffline is not None and diffline.diffline.startswith(" "):
                i2 = diffline.fromline + 1
                j2 = diffline.toline + 1
                diffline = next(it, None)
            yield Edit(i1=i1, j1=j1, i2=i2, j2=j2)
        else:
            if first:
                assert diffline.fromline == diffline.toline, diffline
                if diffline.fromline >= 1:
                    yield Edit(
                        i1=0,
                        j1=0,
                        i2=diffline.fromline,
                        j2=diffline.toline,
                    )
            first = False
            i1 = diffline.fromline
            j1 = diffline.toline
            fromlines: list[str] = []
            tolines: list[str] = []
            while diffline is not None and not diffline.diffline.startswith(" "):
                assert diffline.fromline == i1 + len(fromlines), (
                    diffline,
                    i1,
                    len(fromlines),
                )
                assert diffline.toline == j1 + len(tolines)
                if diffline.diffline.startswith("-"):
                    fromlines.append(diffline.diffline[1:])
                else:
                    assert diffline.diffline.startswith("+")
                    tolines.append(diffline.diffline[1:])
                diffline = next(it, None)
            i2 = i1 + len(fromlines)
            j2 = j1 + len(tolines)
            yield Edit(
                i1=i1,
                j1=j1,
                i2=i2,
                j2=j2,
                fromlines=tuple(fromlines),
                tolines=tuple(tolines),
            )
    # Yield special "eof" Edit (two empty ranges old[i2:i2] to new[j2:j2])
    yield Edit(i1=i2, j1=j2, i2=i2, j2=j2)


def split_diff(diff_a: Iterator[str], diff_b: Iterator[str]):
    it_a = opcodes_from_difflines(diff_parser(diff_a))
    it_b = opcodes_from_difflines(diff_parser(diff_b))
    edit_a = next(it_a)
    edit_b = next(it_b)
    # We create 4 output patches that should be applied in order.
    out1: list[Edit] = []  # Edits from B that do not conflict with A
    out2: list[Edit] = []  # Edits from A that have a conflict with B
    out3: list[Edit] = []  # Edits from B that have a conflict with A
    out4: list[Edit] = []  # Edits from A that do not conflict with B
    # The edits are originally applied in the order of first A, then B.
    # When we split edits into the four patches, we need to apply an offset
    # to the line numbers in the hunk headers to take into account that
    # the hunks are now applied in a different order than before.
    out1offi = 0
    out1offj = 0
    out2offi = 0
    out2offj = 0
    out3offi = 0
    out3offj = 0
    out4offi = 0
    out4offj = 0
    while not edit_a.eof or not edit_b.eof:
        # Check if non-equal edit_a's "new range" (j1..j2) fits into equal edit_b's "old range" (i1..i2).
        # Note that if any of edit_a or edit_b are at eof, it means the other edit
        # was NOT inside an equal range, meaning it cannot be moved.
        if (
            not edit_a.eof
            and not edit_b.eof
            and not edit_a.equal
            and (edit_b.equal and edit_b.i1 <= edit_a.j1 and edit_a.j2 <= edit_b.i2)
        ):
            if HTIME_DEBUG:
                print(
                    f"out4 {edit_b.i1} <= {edit_a.j1} < {edit_a.j2} <= {edit_b.i2} {repr(edit_a.patchlines()[:60])} {out1offi} {out1offj} {out2offi} {out2offj} {out3offi} {out3offj} {out4offi} {out4offj}"
                )
            # Put edit_a in the 4th list (moved down below).
            out4.append(edit_a.offset(out4offi, out4offj))
            # Stuff that goes in out1,out2,out3 needs to
            # take into account that this patch is NO LONGER applied,
            # so their offsets are DECREASED by this hunk's length.
            out1offi -= edit_a.net_added()
            out1offj -= edit_a.net_added()
            out3offi -= edit_a.net_added()
            out3offj -= edit_a.net_added()
            out2offj -= edit_a.net_added()
            edit_a = next(it_a)
        # Check if non-equal edit_b's "old range" (i1..i2) fits into equal edit_a's "new range" (j1..j2)
        elif (
            not edit_a.eof
            and not edit_b.eof
            and not edit_b.equal
            and (edit_a.equal and edit_a.j1 <= edit_b.i1 and edit_b.i2 <= edit_a.j2)
        ):
            if HTIME_DEBUG:
                print(
                    f"out1 {repr(edit_b.patchlines()[:60])} {out1offi} {out1offj} {out2offi} {out2offj} {out3offi} {out3offj} {out4offi} {out4offj}"
                )
            # Put edit_b in the 1st list (moved up above).
            out1.append(edit_b.offset(out1offi, out1offj))
            # Stuff that goes in out2,out4 needs to
            # take into account that this patch is now applied BEFORE,
            # so their offsets are INCREASED by this hunk's length.
            out2offi += edit_b.net_added()
            out2offj += edit_b.net_added()
            out4offi += edit_b.net_added()
            out4offj += edit_b.net_added()
            edit_b = next(it_b)
        # Check if edit_a's "new range" (j1..j2) ends before edit_b's "old range" (i1..i2)
        elif edit_b.eof or (not edit_a.eof and edit_a.j2 < edit_b.i2):
            if not edit_a.equal:
                if HTIME_DEBUG:
                    print(
                        f"out2 {repr(edit_a.patchlines()[:60])} {out1offi} {out1offj} {out2offi} {out2offj} {out3offi} {out3offj} {out4offi} {out4offj}"
                    )
                # Put edit_a in the 2nd list (could not move down).
                out2.append(edit_a.offset(out2offi, out2offj))
                # Stuff that goes in out1 needs to take into account
                # that this patch is now applied AFTER,
                # so their offsets are DECREASED by this hunk's length.
                out1offi -= edit_a.net_added()
                out1offj -= edit_a.net_added()
            edit_a = next(it_a)
        # Check if edit_b's "old range" (i1..i2) ends before edit_a's "new range" (j1..j2)
        elif edit_a.eof or (not edit_b.eof and edit_b.i2 < edit_a.j2):
            if not edit_b.equal:
                if HTIME_DEBUG:
                    print(
                        f"out3 {repr(edit_b.patchlines()[:60])} {out1offi} {out1offj} {out2offi} {out2offj} {out3offi} {out3offj} {out4offi} {out4offj}"
                    )
                # Put edit_b in the 3rd list (could not move up).
                out3.append(edit_b.offset(out3offi, out3offj))
                # Stuff that goes in out4 needs to take into account
                # that this patch is now applied BEFORE,
                # so their offsets are INCREASED by this hunk's length.
                out4offi += edit_b.net_added()
                out4offj += edit_b.net_added()
                # Stuff that goes in out1 needs to take into account
                # that this patch is now applied AFTER,
                # so their offsets are DECREASED by this hunk's length.
                out1offi -= edit_b.net_added()
                out1offj -= edit_b.net_added()
            edit_b = next(it_b)
        else:
            # Advance both
            assert not edit_a.eof and not edit_b.eof
            # Ensure that edit_a's "new range" (j1..j2) ends at the same place as edit_b's "old range" (i1..i2)
            assert edit_a.j2 == edit_b.i2
            if HTIME_DEBUG:
                print(
                    f"both {repr(edit_a.patchlines()[:60])} {repr(edit_b.patchlines()[:60])} {out1offi} {out1offj} {out2offi} {out2offj} {out3offi} {out3offj} {out4offi} {out4offj}"
                )
            if not edit_a.equal:
                # Put edit_a in the 2nd list (could not move down).
                out2.append(edit_a.offset(out2offi, out2offj))
                out1offi -= edit_a.net_added()
                out1offj -= edit_a.net_added()
            if not edit_b.equal:
                # Put edit_b in the 3rd list (could not move up).
                out3.append(edit_b.offset(out3offi, out3offj))
                out4offi += edit_b.net_added()
                out4offj += edit_b.net_added()
                out1offi -= edit_b.net_added()
                out1offj -= edit_b.net_added()
            edit_a = next(it_a)
            edit_b = next(it_b)
    patch1 = "".join(e.patchlines() for e in out1)
    patch2 = "".join(e.patchlines() for e in out2)
    patch3 = "".join(e.patchlines() for e in out3)
    patch4 = "".join(e.patchlines() for e in out4)
    return patch1, patch2, patch3, patch4


@subcommand
def htime_swap(lineno: int, up_or_down: Literal["down", "up"]) -> None:
    """Swap the given (1-indexed) line with the neighboring commit line.

    If the two commits touch the same files with merge conflicts, split each
    commit's changes to the shared files into conflicting/clean hunks and
    emit replacement todo lines so the swap can still proceed.

    If the two commits don't make overlapping edits, but simply adjacent edits
    (e.g. commit A modifies line 10 and commit B modifies line 11),
    then this function can be used to swap the two commits
    without actually identifying any conflicting hunks.
    """
    lines = sys.stdin.read().splitlines()
    assert 1 <= lineno <= len(lines)
    targetline = parse_sequencer_line(lines[lineno - 1])
    if targetline is None or not targetline.oid:
        print(json.dumps({"message": "Please put the cursor on a 'pick' line"}))
        return
    dd = 1 if up_or_down == "down" else -1
    ix = lineno - 1 + dd
    extra = 0
    while 0 <= ix < len(lines):
        line = parse_sequencer_line(lines[ix])
        if line is None:
            extra += 1
            ix += dd
            continue
        if not line.oid:
            # exec, reset, merge -> these are dangerous
            errmsg = f"Don't want to move past '{line.verb}' command"
            print(json.dumps({"message": errmsg}))
            return
        break
    else:
        print(json.dumps({"message": "Nothing to swap with"}))
        return
    if up_or_down == "down":
        first, second = targetline, line
        first_lineno, second_lineno = lineno, ix + 1
    else:
        assert up_or_down == "up"
        first, second = line, targetline
        first_lineno, second_lineno = ix + 1, lineno
    firstfiles = [ns.path for ns in git_show_numstat(first.oid).numstat]
    secondfiles = [ns.path for ns in git_show_numstat(second.oid).numstat]
    firstsubj = git_show_commit_subject(first.oid)
    secondsubj = git_show_commit_subject(second.oid)
    onlyfirst = [path for path in firstfiles if path not in secondfiles]
    onlysecond = [path for path in secondfiles if path not in firstfiles]
    bothfiles = [path for path in secondfiles if path in firstfiles]
    conflictmessage = move_conflict(up_or_down, targetline.oid, line.oid, bothfiles)
    if HTIME_DEBUG:
        print(
            "move", up_or_down, targetline.oid, line.oid, bothfiles, conflictmessage
        )
    if conflictmessage is None:
        print(json.dumps({"movelines": 1}))
        return
    if conflictmessage.error != MERGE_CONFLICT_ERROR:
        # Only a plain merge conflict can be worked around by splitting hunks;
        # other reasons (non-commutative, cancelling, ...) abort with a message.
        print(json.dumps({"message": str(conflictmessage)}))
        return
    # Files where we can swap the two commits by relying on ordinary git cherry-pick.
    moveboth = [path for path in bothfiles if path not in conflictmessage.paths]
    if git_any_staged_changes():
        raise SystemExit("refuse to run when there are staged changes")
    head_sha = git_rev_parse_head()
    try:
        # If the two lines are not adjacent commits, replay `second` (shared
        # files only) on top of `first` to obtain `second_treespec`, the tree
        # "as if second followed first", used for the hunk-splitting diff.
        if git_rev_parse(f"{second.oid}^") != git_rev_parse(first.oid):
            git_set_head_and_staging(first.oid, None)
            if git_apply_cached_from_git_show(
                second.oid, file_list=conflictmessage.paths
            ):
                raise SystemExit(
                    f"Cannot apply {second.oid} directly on top of {first.oid}"
                )
            second_treespec = git_write_tree()
        else:
            second_treespec = second.oid
        patch1 = patch2 = patch3 = patch4 = ""
        for path in conflictmessage.paths:
            if HTIME_DEBUG:
                print(
                    f"swap {first.oid} {second_treespec} {path}"
                )
            with (
                subprocess.Popen(
                    ("git", "diff", f"{first.oid}^:{path}", f"{first.oid}:{path}"),
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                ) as first_diff,
                subprocess.Popen(
                    ("git", "diff", f"{first.oid}:{path}", f"{second_treespec}:{path}"),
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                ) as second_diff,
            ):
                assert first_diff.stdout
                assert second_diff.stdout
                p1, p2, p3, p4 = split_diff(first_diff.stdout, second_diff.stdout)
            patchhead = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            if p1:
                patch1 += f"{patchhead}{p1}"
            if p2:
                patch2 += f"{patchhead}{p2}"
            if p3:
                patch3 += f"{patchhead}{p3}"
            if p4:
                patch4 += f"{patchhead}{p4}"
        # Now create up to four commits, in sequence:
        # - Changes from second that do not have a conflict.
        # - Changes from first that have a conflict.
        # - Changes from second that have a conflict.
        # - Changes from first that do not have a conflict.
        git_set_head_and_staging(f"{first.oid}^", None)
        replace_first: list[SequencerLine] = []
        replace_second: list[SequencerLine] = []
        if patch1 or onlysecond or moveboth:
            if patch1:
                git_apply_cached_unidiff_zero_from_str(patch1)
            if onlysecond:
                git_set_staging(second.oid, file_list=onlysecond)
            if moveboth:
                if git_apply_cached_from_git_show(second.oid, file_list=moveboth):
                    raise Exception("unexpected merge conflict (moveboth a)")
            git_commit_with_same_authorship(second.oid)
            replace_first.append(
                targetline.update(
                    verb="pick ", oid=git_rev_parse_head(), suffix=secondsubj
                )
            )
        if patch2:
            git_apply_cached_unidiff_zero_from_str(patch2)
            git_commit_with_same_authorship(first.oid)
            commitmsg = f"Conflicts from: {firstsubj}"
            git_amend_with_commit_msg(commitmsg)
            replace_first.append(
                targetline.update(
                    verb="pick ", oid=git_rev_parse_head(), suffix=commitmsg
                )
            )
        if patch3:
            git_apply_cached_unidiff_zero_from_str(patch3)
            git_commit_with_same_authorship(second.oid)
            commitmsg = f"Conflicts from: {secondsubj}"
            git_amend_with_commit_msg(commitmsg)
            replace_second.append(
                targetline.update(
                    verb="pick ", oid=git_rev_parse_head(), suffix=commitmsg
                )
            )
        if patch4 or onlyfirst or moveboth:
            if patch4:
                git_apply_cached_unidiff_zero_from_str(patch4)
            if onlyfirst:
                git_set_staging(first.oid, file_list=onlyfirst)
            if moveboth:
                if git_apply_cached_from_git_show(first.oid, file_list=moveboth):
                    raise Exception("unexpected merge conflict (moveboth b)")
            git_commit_with_same_authorship(first.oid)
            replace_second.append(
                targetline.update(
                    verb="pick ", oid=git_rev_parse_head(), suffix=firstsubj
                )
            )
        # The resulting file after the new four commits
        # should be equal to the file after the original two commits.
        assert git_is_same("@", second_treespec), (
            f"git diff {git_rev_parse_head()} {second_treespec}"
        )

        # Emit a Vim command to move the cursor without updating the jump list.
        cursormove = len(replace_first) + len(replace_second) + extra - 1
        if up_or_down == "down":
            movement = f"norm {cursormove}j"
        else:
            movement = f"norm {cursormove}k"
        replace_second_line = {
            "lineno": second_lineno,
            "s": "\n".join(map(str, replace_second)),
        }
        replace_first_line = {
            "lineno": first_lineno,
            "s": "\n".join(map(str, replace_first)),
        }
        # Emit replacements in the order they should be applied, namely
        # second before first, as the line numbers below the replacements
        # may shift around.
        replacements = [replace_second_line, replace_first_line]
        print(json.dumps({"replacements": replacements, "command": movement}))
    finally:
        git_set_head_and_staging(head_sha, None)


if __name__ == "__main__":
    main()
