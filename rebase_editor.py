#!/usr/bin/env python3
"""
Git rebase interactive sequence editor TUI.

Use as GIT_SEQUENCE_EDITOR:
    GIT_SEQUENCE_EDITOR=rebase_editor.py git rebase -i HEAD~5
"""
import curses
import os
import subprocess
import sys
from typing import Literal

# Allow importing from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from htime import git_show_numstat, move_conflict, parse_sequencer_line


# ---------------------------------------------------------------------------
# Colour scheme
# ---------------------------------------------------------------------------
# Pair numbers 1-10 are reserved for the editor.
PAIR_PICK = 1
PAIR_REWORD = 2
PAIR_FIXUP = 3
PAIR_SQUASH = 4
PAIR_EDIT_VERB = 5
PAIR_BREAK = 6
PAIR_EXEC = 7
PAIR_DROPPED = 8
PAIR_EDITMODE = 9
PAIR_STATUS = 10
PAIR_DIVIDER = 11
PAIR_DIFF_ADD = 12
PAIR_DIFF_DEL = 13
PAIR_DIFF_HUNK = 14
PAIR_DIFF_META = 15


def diff_color(line: str) -> int:
    if line.startswith("+") and not line.startswith("+++"):
        return PAIR_DIFF_ADD
    if line.startswith("-") and not line.startswith("---"):
        return PAIR_DIFF_DEL
    if line.startswith("@@"):
        return PAIR_DIFF_HUNK
    if line.startswith(("diff ", "index ", "--- ", "+++ ", "commit ", "Author:", "Date:")):
        return PAIR_DIFF_META
    return 0


def verb_color(line: str) -> int:
    s = line.lstrip()
    if s.startswith("#"):
        return PAIR_DROPPED
    if s.startswith(("pick ", "p ")):
        return PAIR_PICK
    if s.startswith(("reword ", "r ")):
        return PAIR_REWORD
    if s.startswith(("fixup ", "f ")):
        return PAIR_FIXUP
    if s.startswith(("squash ", "s ")):
        return PAIR_SQUASH
    if s.startswith(("edit ", "e ")):
        return PAIR_EDIT_VERB
    if s.startswith("break"):
        return PAIR_BREAK
    if s.startswith(("exec ", "x ")):
        return PAIR_EXEC
    return 0


# ---------------------------------------------------------------------------
# Line manipulation helpers
# ---------------------------------------------------------------------------


def change_verb(line: str, new_verb: str) -> str:
    """Replace the verb of a sequencer line (new_verb must end with a space)."""
    parsed = parse_sequencer_line(line.rstrip("\n"))
    if parsed is None or not parsed.oid:
        return line
    updated = parsed.update(verb=new_verb)
    result = str(updated)
    if line.endswith("\n"):
        result += "\n"
    return result


def toggle_comment(line: str) -> str:
    """
    Toggle the commented-out state of a line.
    """
    stripped = line.rstrip("\n")
    nl = "\n" if line.endswith("\n") else ""
    if stripped.startswith("# "):
        return stripped[2:] + nl
    return "# " + stripped + nl


# ---------------------------------------------------------------------------
# htime-based move-distance calculation
# ---------------------------------------------------------------------------

def htime_move_steps(
    lines: list[str], idx: int, direction: str
) -> tuple[int, str | None]:
    """
    Return (steps, error_message).

    steps > 0 means the line at *idx* can be moved that many positions in
    *direction* ('up' or 'down').  steps==0 means it cannot move.
    """
    parsed = parse_sequencer_line(lines[idx].rstrip("\n"))
    if parsed is None or not parsed.oid:
        return 0, "Not a commit line"
    try:
        movefiles = [ns.path for ns in git_show_numstat(parsed.oid).numstat]
    except Exception as exc:
        return 0, str(exc)

    dd = 1 if direction == "down" else -1
    ix = idx + dd
    extra = 0
    mv = 0
    conflictmessage: str | None = None

    while 0 <= ix < len(lines):
        line = parse_sequencer_line(lines[ix].rstrip("\n"))
        if line is None:
            extra += 1
            ix += dd
            continue
        if not line.oid:
            conflictmessage = f"Cannot move past '{line.verb.strip()}' command"
            break
        conflictmessage = move_conflict(direction, parsed.oid, line.oid, movefiles)
        if conflictmessage is not None:
            break
        mv += extra + 1
        extra = 0
        ix += dd

    if mv == 0:
        return 0, conflictmessage
    return mv, None


def move_line(lines: list[str], idx: int, dd: int, steps: int) -> tuple[list[str], int]:
    """Physically move lines[idx] by *steps* positions in direction dd (+1/-1)."""
    for _ in range(steps):
        nxt = idx + dd
        if 0 <= nxt < len(lines):
            lines[idx], lines[nxt] = lines[nxt], lines[idx]
            idx = nxt
    return lines, idx


# ---------------------------------------------------------------------------
# git show fetching (cached per oid)
# ---------------------------------------------------------------------------

def fetch_git_show(oid: str) -> list[str]:
    """Run 'git show <oid>' and return the output as a list of lines."""
    try:
        result = subprocess.run(
            ["git", "show", "--color=never", oid],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return result.stdout.splitlines()
    except Exception as exc:
        return [str(exc)]


def left_width(total_w: int) -> int:
    """Width of the left (list) pane, not counting the divider column."""
    return max(20, total_w // 2 - 1)


# ---------------------------------------------------------------------------
# Screen drawing
# ---------------------------------------------------------------------------

def draw(
    stdscr: "curses._CursesWindow",
    lines: list[str],
    cursor: int,
    left_top: int,
    show_lines: list[str],
    right_top: int,
    focus: Literal["left", "right"],
    status: str,
    edit_mode: bool,
    edit_buf: str,
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    content_rows = h - 1  # last row is status bar
    lw = left_width(w)
    div_col = lw + 1        # column of the divider '│'
    rw = w - div_col - 1   # width of right pane

    # ---- left pane --------------------------------------------------------
    for row in range(content_rows):
        li = left_top + row
        if li >= len(lines):
            break
        display = lines[li].rstrip("\n")[:lw]
        color = verb_color(lines[li])
        attr = curses.color_pair(color)
        if li == cursor:
            if edit_mode:
                attr = curses.color_pair(PAIR_EDITMODE) | curses.A_BOLD
                display = edit_buf[:lw]
            else:
                attr = attr | curses.A_REVERSE
                if focus == "left":
                    attr = attr | curses.A_BOLD
        try:
            stdscr.addstr(row, 0, display.ljust(lw), attr)
        except curses.error:
            pass

    # ---- divider ----------------------------------------------------------
    div_attr = curses.color_pair(PAIR_DIVIDER)
    for row in range(content_rows):
        try:
            stdscr.addch(row, div_col, curses.ACS_VLINE, div_attr)
        except curses.error:
            pass

    # ---- right pane -------------------------------------------------------
    for row in range(content_rows):
        ri = right_top + row
        if ri >= len(show_lines):
            break
        display = show_lines[ri][:rw]
        attr = curses.color_pair(diff_color(show_lines[ri]))
        if focus == "right" and ri == right_top:
            attr = attr | curses.A_REVERSE
        try:
            stdscr.addstr(row, div_col + 1, display.ljust(rw), attr)
        except curses.error:
            pass

    # ---- status bar -------------------------------------------------------
    focus_indicator = "[list]" if focus == "left" else "[show]"
    bar = f"{focus_indicator} {status}"
    try:
        stdscr.addstr(
            h - 1, 0,
            bar[: w - 1].ljust(w - 1),
            curses.color_pair(PAIR_STATUS) | curses.A_BOLD,
        )
    except curses.error:
        pass

    if edit_mode and focus == "left":
        row = cursor - left_top
        col = min(len(edit_buf), lw - 1)
        try:
            stdscr.move(row, col)
        except curses.error:
            pass

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Inline exec-command prompt
# ---------------------------------------------------------------------------

def prompt_exec(stdscr: "curses._CursesWindow") -> str | None:
    """Show an inline prompt and return the entered command, or None."""
    h, w = stdscr.getmaxyx()
    prompt = "exec command: "
    buf = ""
    curses.curs_set(1)
    while True:
        try:
            stdscr.addstr(
                h - 1, 0,
                (prompt + buf)[: w - 1].ljust(w - 1),
                curses.color_pair(PAIR_STATUS) | curses.A_BOLD,
            )
            stdscr.move(h - 1, min(len(prompt) + len(buf), w - 2))
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.get_wch()
        if key == '\x1b':  # Esc - cancel
            curses.curs_set(0)
            return None
        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            curses.curs_set(0)
            return buf.strip() or None
        if key in (curses.KEY_BACKSPACE, 127, "\x7f"):
            buf = buf[:-1]
        elif isinstance(key, str):
            buf += key


# ---------------------------------------------------------------------------
# Main TUI loop
# ---------------------------------------------------------------------------

_HELP = (
    "↑↓/PgUp/PgDn=cursor  p/f/c/s=verb  x=toggle#  X=delete  b/B=break  e/E=exec  "
    "u/d=move(safe)  U/D=move(force)  j/k=move far  m=mark  z=undo  i=edit  Tab=pane  R=run  Esc=cancel"
)


def tui(stdscr: "curses._CursesWindow", todo_file: str) -> int:
    with open(todo_file) as fh:
        lines = fh.readlines()

    # Ensure every line ends with \n for consistency
    lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PAIR_PICK,      curses.COLOR_GREEN,   -1)
    curses.init_pair(PAIR_REWORD,    curses.COLOR_YELLOW,  -1)
    curses.init_pair(PAIR_FIXUP,     curses.COLOR_CYAN,    -1)
    curses.init_pair(PAIR_SQUASH,    curses.COLOR_MAGENTA, -1)
    curses.init_pair(PAIR_EDIT_VERB, curses.COLOR_RED,     -1)
    curses.init_pair(PAIR_BREAK,     curses.COLOR_WHITE,   -1)
    curses.init_pair(PAIR_EXEC,      curses.COLOR_BLUE,    -1)
    curses.init_pair(PAIR_DROPPED,   curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(PAIR_EDITMODE,  curses.COLOR_BLACK,   curses.COLOR_YELLOW)
    curses.init_pair(PAIR_STATUS,    curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(PAIR_DIVIDER,   curses.COLOR_WHITE,   -1)
    curses.init_pair(PAIR_DIFF_ADD,   curses.COLOR_GREEN,   -1)
    curses.init_pair(PAIR_DIFF_DEL,   curses.COLOR_RED,     -1)
    curses.init_pair(PAIR_DIFF_HUNK,  curses.COLOR_CYAN,    -1)
    curses.init_pair(PAIR_DIFF_META,  curses.COLOR_YELLOW,  -1)

    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    curses.curs_set(0)
    curses.set_escdelay(25)  # ms - make Esc respond immediately

    # Start cursor on first non-comment, non-empty line
    cursor = 0
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            cursor = i
            break

    left_top = 0
    right_top = 0
    focus: Literal["left", "right"] = "left"
    status = _HELP
    edit_mode = False
    edit_buf = ""
    history: list[tuple[list[str], int]] = []

    # Cache for git show output keyed by oid
    show_cache: dict[str, list[str]] = {}
    last_oid: str | None = None
    show_lines: list[str] = []

    def push_history() -> None:
        history.append((list(lines), cursor))

    def scroll_left_to(cur: int) -> None:
        nonlocal left_top
        h, _ = stdscr.getmaxyx()
        visible = h - 1
        if cur < left_top:
            left_top = cur
        elif cur >= left_top + visible:
            left_top = cur - visible + 1

    def refresh_show(cur: int) -> None:
        nonlocal show_lines, right_top, last_oid
        parsed = parse_sequencer_line(lines[cur].rstrip("\n"))
        oid = parsed.oid if parsed else None
        if not oid:
            show_lines = ["(no commit on this line)"]
            right_top = 0
            last_oid = None
            return
        if oid == last_oid:
            return
        last_oid = oid
        right_top = 0
        if oid not in show_cache:
            show_cache[oid] = fetch_git_show(oid)
        show_lines = show_cache[oid]

    while True:
        refresh_show(cursor)
        scroll_left_to(cursor)
        draw(
            stdscr, lines, cursor, left_top,
            show_lines, right_top,
            focus, status, edit_mode, edit_buf,
        )
        key = stdscr.get_wch()

        # ---------------------------------------------------------------
        # Edit mode
        # ---------------------------------------------------------------
        if edit_mode:
            if key == '\x1b':  # Esc - discard
                edit_mode = False
                curses.curs_set(0)
                status = _HELP
            elif key in ("\n", "\r", 10, 13, curses.KEY_ENTER):
                lines[cursor] = edit_buf + "\n"
                edit_mode = False
                curses.curs_set(0)
                status = "Line updated"
            elif key in (curses.KEY_BACKSPACE, 127, "\x7f"):
                edit_buf = edit_buf[:-1]
            elif isinstance(key, str) and key.isprintable():
                edit_buf += key
            continue

        # Global keys (work in both panes)
        if key == "R":
            with open(todo_file, "w") as fh:
                fh.writelines(lines)
            return 0
        elif key == "\t":
            focus = "right" if focus == "left" else "left"
            status = _HELP
            continue

        # ---------------------------------------------------------------
        # Right-pane focus: only scroll and global keys
        # ---------------------------------------------------------------
        if focus == "right":
            h, _ = stdscr.getmaxyx()
            visible = h - 1
            page = max(1, visible - 1)
            if key == curses.KEY_UP:
                right_top = max(0, right_top - 1)
            elif key == curses.KEY_DOWN:
                right_top = min(max(0, len(show_lines) - 1), right_top + 1)
            elif key == curses.KEY_PPAGE:
                right_top = max(0, right_top - page)
            elif key == curses.KEY_NPAGE:
                right_top = min(max(0, len(show_lines) - 1), right_top + page)
            elif key == '\x1b':
                return 1
            continue

        # ---------------------------------------------------------------
        # Left-pane (normal) mode
        # ---------------------------------------------------------------
        if key == curses.KEY_UP:
            if cursor > 0:
                cursor -= 1
            status = _HELP

        elif key == curses.KEY_DOWN:
            if cursor < len(lines) - 1:
                cursor += 1
            status = _HELP

        elif key == curses.KEY_PPAGE:  # Page Up
            h, _ = stdscr.getmaxyx()
            page = max(1, h - 2)
            cursor = max(0, cursor - page)
            status = _HELP

        elif key == curses.KEY_NPAGE:  # Page Down
            h, _ = stdscr.getmaxyx()
            page = max(1, h - 2)
            cursor = min(len(lines) - 1, cursor + page)
            status = _HELP

        elif key == '\x1b' or key == curses.KEY_EXIT or key == 'q':  # Esc - cancel rebase
            return 1

        elif key == "i":
            edit_buf = lines[cursor].rstrip("\n")
            edit_mode = True
            curses.curs_set(1)
            status = "Edit mode - Enter to save, Esc to cancel"

        elif key == "x":
            push_history()
            lines[cursor] = toggle_comment(lines[cursor])
            status = "Toggled"

        elif key == "X":
            if len(lines) > 1:
                push_history()
                lines.pop(cursor)
                cursor = min(cursor, len(lines) - 1)
                status = "Line deleted"
            else:
                status = "Cannot delete the last line"

        elif key == "p":
            push_history()
            lines[cursor] = change_verb(lines[cursor], "pick ")
            status = "→ pick"

        elif key == "f":
            push_history()
            lines[cursor] = change_verb(lines[cursor], "f ")
            status = "→ fixup"

        elif key == "c":
            push_history()
            lines[cursor] = change_verb(lines[cursor], "f -C ")
            status = "→ fixup -C"

        elif key == "s":
            push_history()
            lines[cursor] = change_verb(lines[cursor], "s ")
            status = "→ squash"

        elif key == "b":
            push_history()
            lines.insert(cursor, "break\n")
            cursor += 1  # cursor stays on original line
            status = "Break inserted above"

        elif key == "B":
            push_history()
            lines.insert(cursor + 1, "break\n")
            status = "Break inserted below"

        elif key == "e":
            cmd = prompt_exec(stdscr)
            if cmd:
                push_history()
                lines.insert(cursor, f"exec {cmd}\n")
                cursor += 1
                status = f"exec {cmd!r} inserted above"
            else:
                status = "Cancelled"

        elif key == "E":
            cmd = prompt_exec(stdscr)
            if cmd:
                push_history()
                lines.insert(cursor + 1, f"exec {cmd}\n")
                status = f"exec {cmd!r} inserted below"
            else:
                status = "Cancelled"

        elif key == "u":
            steps, msg = htime_move_steps(lines, cursor, "up")
            if steps > 0:
                push_history()
                lines, cursor = move_line(lines, cursor, -1, 1)
                status = "Moved up"
            else:
                status = msg or "Cannot move up"

        elif key == "d":
            steps, msg = htime_move_steps(lines, cursor, "down")
            if steps > 0:
                push_history()
                lines, cursor = move_line(lines, cursor, 1, 1)
                status = "Moved down"
            else:
                status = msg or "Cannot move down"

        elif key == "U":
            if cursor > 0:
                push_history()
                lines[cursor], lines[cursor - 1] = lines[cursor - 1], lines[cursor]
                cursor -= 1
                status = "Moved up (forced)"
            else:
                status = "Already at top"

        elif key == "D":
            if cursor < len(lines) - 1:
                push_history()
                lines[cursor], lines[cursor + 1] = lines[cursor + 1], lines[cursor]
                cursor += 1
                status = "Moved down (forced)"
            else:
                status = "Already at bottom"

        elif key == "k":
            steps, msg = htime_move_steps(lines, cursor, "up")
            if steps > 0:
                push_history()
                lines, cursor = move_line(lines, cursor, -1, steps)
                status = f"Moved up {steps} position(s)"
            else:
                status = msg or "Cannot move further up"

        elif key == "j":
            steps, msg = htime_move_steps(lines, cursor, "down")
            if steps > 0:
                push_history()
                lines, cursor = move_line(lines, cursor, 1, steps)
                status = f"Moved down {steps} position(s)"
            else:
                status = msg or "Cannot move further down"

        elif key == "z":
            if history:
                lines, cursor = history.pop()
                status = "Undone"
            else:
                status = "Nothing to undo"

        elif key == "m":
            if cursor + 1 < len(lines):
                push_history()
                lines[cursor] = change_verb(lines[cursor], "f -C ")
                lines[cursor + 1] = change_verb(lines[cursor + 1], "f ")
                status = "Marked: f -C (current) / f (below)"
            else:
                status = "No line below to mark"


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <rebase-todo-file>", file=sys.stderr)
        return 1
    return curses.wrapper(tui, sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
