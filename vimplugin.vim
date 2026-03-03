let s:path = expand('<sfile>:p:h')

exec 'py3file '..s:path..'/vimplugin.py'

function HtimeEdit()
	let l:rebaseline = getline('.')
	let l:rebasebuf = bufnr()
	let l:rebasewin = winnr()
	if l:rebaseline =~ '^[presf]'
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

function HtimeEditWrite()
	let l:commitbuf = bufnr()
	let l:commitwin = winnr()
	let l:rebaseline = b:rebaseline
	let l:rebasebuf = b:rebasebuf
	let l:rebasewin = b:rebasewin
	exec 'silent %!python3 '.s:path.'/htime.py write --rebaseline '.shellescape(l:rebaseline, 1).' || :'
	let l:result = getline('$')
	if l:result =~ '^{'
		set nomodified
		exec 'silent buffer! '.l:rebasebuf
		exec 'silent %!python3 '.s:path.'/htime.py update --rebaseline '.shellescape(l:rebaseline, 1).' --result '.shellescape(l:result, 1).' || :'
		enew
	endif
endfunction

au FileType gitrebase nnoremap <silent> <buffer> <Leader>j :py3 htime_move("down")<CR>
au FileType gitrebase nnoremap <silent> <buffer> <Leader>k :py3 htime_move("up")<CR>
au FileType gitrebase nnoremap <silent> <buffer> <CR> :call HtimeEdit()<CR>
au FileType gitrebase nnoremap <silent> <buffer> <Leader>\ :py3 htime_cleanup()<CR>
