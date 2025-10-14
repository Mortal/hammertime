# Reorder git rebase -i commits safely

The `move_sequencer_line.py` script can be used on a "git rebase todo list" to move a commit as far up or down as it can go in the sequence.

The script takes three arguments:

`python3 move_sequencer_line.py <'up'|'down'> <lineno> <git-rebase-todo-path>`

...where the first argument is whether to move the given line up or down,
the second argument is the (1-indexed) line number of the line to move,
and the third argument is the path to your `.git/rebase-merge/git-rebase-todo` file.

If the third argument is `-`, the file is read from stdin and the result written on stdout.
Otherwise, the file is read, modified, and written back out.

If you use Vim, you can use the following autocommands to bind the script to `\j` and `\k`:

```
au FileType gitrebase nnoremap <buffer> <Leader>j :exec '%!python3 ~/work/hammertime/move_sequencer_line.py down '.line('.').' -'<CR>
au FileType gitrebase nnoremap <buffer> <Leader>k :exec '%!python3 ~/work/hammertime/move_sequencer_line.py up '.line('.').' -'<CR>
```
