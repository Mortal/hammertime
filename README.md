# Reorder git rebase -i commits safely

The `htime.py` script can be used on a "git rebase todo list" to edit diffs safely, and to move a commit as far up or down as it can go in the sequence.

* `htime.py open "pick COMMIT"` - output the diff of `COMMIT`.
* `htime.py update "pick COMMIT" "$(htime.py write "pick COMMIT" < patch)" < git-rebase-todo` - update git-rebase-todo, replacing the "pick COMMIT" line with a new line matching the diff in "patch".
* `htime.py move <lineno> <'up'|'down'> < git-rebase-todo` - update git-rebase-todo, moving the given line (1-indexed) as far up or down as it can go.

The script's command-line interface is designed to be integrated into Vim, but hopefully the interface is general enough that the Vim-specific plugin can be easily ported to another editor.

To install the Vim plugin, add the following to your `.vimrc`:

```
source ~/path/to/hammertime/vimplugin.vim
```

The Vim plugin binds the following three keys in the `gitrebase` filetype (used for the git rebase todo list):

* `\j` - move current line as far down as possible.
* `\k` - move current line as far up as possible.
* Enter - open the current line's diff in a new patch, which can be edited and saved with `:wq`.
