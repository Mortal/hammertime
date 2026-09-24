" Hammertime Vim plugin: buffers and key bindings for editing a git rebase
" todo list with htime.py (see README.md). Loaded via :py3file below.
let s:path = expand('<sfile>:p:h')

exec 'py3file '..s:path..'/vimplugin.py'

" Open the commit under the cursor in a new scratch buffer for hand-editing.
function HtimeEdit()
	let l:rebaseline = getline('.')
	let l:rebasebuf = bufnr()
	let l:rebasewin = winnr()
	if l:rebaseline =~ '^[presf]\>\|^pick\|^reword\|^edit\|^squash\|^fixup'
		new
		let b:rebaseline = l:rebaseline
		let b:rebasebuf = l:rebasebuf
		let b:rebasewin = l:rebasewin
		exec "silent file ".fnameescape('HAMMERTIME: '.l:rebaseline)
		set buftype=acwrite ft=git modifiable
		exec '%!python3 '.s:path.'/htime.py open --rebaseline '.shellescape(l:rebaseline, 1).' || :'
		set nomodified
		autocmd BufWriteCmd <buffer> :call HtimeEditWrite()
	endif
endfunction

" On :w in the scratch buffer: feed the edited patch to "htime write",
" then update the todo list buffer with the result of "htime update".
function HtimeEditWrite()
	let l:rebaseline = b:rebaseline
	let l:rebasebuf = b:rebasebuf
	let l:rebasewin = b:rebasewin
	exec 'silent %!python3 '.s:path.'/htime.py write --rebaseline '.shellescape(l:rebaseline, 1).' || :'
	let l:result = getline('$')
	if l:result =~ '^{'
		set nomodified
		exec 'silent buffer! '.l:rebasebuf
		exec 'silent %!python3 '.s:path.'/htime.py update --rebaseline '.shellescape(l:rebaseline, 1).' --result '.shellescape(l:result, 1).' || :'
		" Unfortunately we cannot force-close the window after writing,
		" so in case the user uses ":w" instead of ":wq", we use :enew
		" here to leave a new empty buffer behind.
		enew
	endif
endfunction

" Key bindings in gitrebase buffers:
" <Leader>j/k move the line down/up, Enter edits the commit's diff.
au FileType gitrebase nnoremap <silent> <buffer> <Leader>j :py3 htime_move("down")<CR>
au FileType gitrebase nnoremap <silent> <buffer> <Leader>k :py3 htime_move("up")<CR>
au FileType gitrebase nnoremap <silent> <buffer> <CR> :call HtimeEdit()<CR>
