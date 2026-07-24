" claude-pair vim integration
" Writes the current file, cursor position, and buffer lines around the
" cursor (including unsaved edits) to a state file that the claude-pair
" watcher reads. No network calls happen from vim.

if exists('g:loaded_claude_pair') || &compatible
  finish
endif
let g:loaded_claude_pair = 1

let g:claude_pair_context_lines = get(g:, 'claude_pair_context_lines', 60)
let g:claude_pair_enabled = get(g:, 'claude_pair_enabled', 1)

let s:cache_home = empty($XDG_CACHE_HOME) ? expand('~/.cache') : $XDG_CACHE_HOME
let s:state_dir = s:cache_home . '/claude-pair'
let s:state_file = s:state_dir . '/vim_state.json'

function! s:WriteState() abort
  if !g:claude_pair_enabled || empty(expand('%')) || !empty(&buftype)
    return
  endif
  if !isdirectory(s:state_dir)
    call mkdir(s:state_dir, 'p')
  endif
  let l:lnum = line('.')
  let l:half = g:claude_pair_context_lines / 2
  let l:first = max([1, l:lnum - l:half])
  let l:last = min([line('$'), l:lnum + l:half])
  let l:state = {
        \ 'file': expand('%:p'),
        \ 'filetype': &filetype,
        \ 'line': l:lnum,
        \ 'col': col('.'),
        \ 'mode': mode(),
        \ 'modified': &modified ? 1 : 0,
        \ 'first_line': l:first,
        \ 'context': getline(l:first, l:last),
        \ 'ts': localtime(),
        \ }
  call writefile([json_encode(l:state)], s:state_file)
endfunction

" ring of recently edited files; the watcher auto-loads their saved
" contents as reference context (claude-pair --vim-files N, default 5)
let g:claude_pair_recent_files = get(g:, 'claude_pair_recent_files', 10)
let s:recent_file = s:state_dir . '/vim_recent.json'
let s:recent = []

function! s:TrackRecent() abort
  if !g:claude_pair_enabled || empty(expand('%')) || !empty(&buftype)
        \ || g:claude_pair_recent_files <= 0
    return
  endif
  let l:path = expand('%:p')
  call filter(s:recent, 'v:val !=# l:path')
  call insert(s:recent, l:path, 0)
  if len(s:recent) > g:claude_pair_recent_files
    call remove(s:recent, g:claude_pair_recent_files, -1)
  endif
  if !isdirectory(s:state_dir)
    call mkdir(s:state_dir, 'p')
  endif
  call writefile([json_encode(s:recent)], s:recent_file)
endfunction

augroup ClaudePair
  autocmd!
  " CursorHold fires after 'updatetime' ms of idleness; consider
  " `set updatetime=1000` so state stays fresh while you pause.
  autocmd CursorHold,CursorHoldI,BufEnter,BufWritePost,InsertLeave * call s:WriteState()
  autocmd BufEnter,BufWritePost * call s:TrackRecent()
augroup END

command! ClaudePairToggle let g:claude_pair_enabled = !g:claude_pair_enabled
      \ | echo 'claude-pair vim state: ' . (g:claude_pair_enabled ? 'on' : 'off')

" --- send the whole current file to claude-pair as reference context ------

function! s:SendContext() abort
  if empty(expand('%')) || !empty(&buftype)
    echo 'claude-pair: no file to send'
    return
  endif
  let l:dir = s:state_dir . '/context'
  if !isdirectory(l:dir)
    call mkdir(l:dir, 'p')
  endif
  let l:src = expand('%:p')
  " deterministic name (matches the CLI's slug) so re-sending replaces
  let l:slug = substitute(l:src, '[^A-Za-z0-9._-]', '_', 'g')
  let l:slug = strpart(l:slug, max([0, len(l:slug) - 120]))
  let l:body = ['# source: ' . l:src, '=== ' . l:src . ' ==='] + getline(1, '$')
  call writefile(l:body, l:dir . '/' . l:slug . '.ctx')
  echo 'claude-pair: sent ' . line('$') . ' lines as context'
endfunction

command! ClaudeContext call s:SendContext()

nnoremap <silent> <Plug>(ClaudePairContext) :call <SID>SendContext()<CR>
if get(g:, 'claude_pair_default_mappings', 1)
      \ && !hasmapto('<Plug>(ClaudePairContext)') && empty(maparg('<Leader>cc', 'n'))
  nmap <Leader>cc <Plug>(ClaudePairContext)
endif

" --- paste the latest suggestion's code at the cursor ----------------------

function! s:PasteLast() abort
  let l:file = s:state_dir . '/last_code.txt'
  if !filereadable(l:file)
    echo 'claude-pair: no code in the last suggestion (:ClaudeLastShow for the full text)'
    return
  endif
  let l:lines = readfile(l:file)
  " drop trailing blank lines
  while !empty(l:lines) && l:lines[-1] =~# '^\s*$'
    call remove(l:lines, -1)
  endwhile
  if empty(l:lines)
    echo 'claude-pair: no code in the last suggestion (:ClaudeLastShow for the full text)'
    return
  endif
  call append(line('.'), l:lines)
  execute 'normal! ' . (line('.') + 1) . 'G'
  echo 'claude-pair: inserted ' . len(l:lines) . ' line(s)'
endfunction

" --- show the full latest suggestion in a scratch split --------------------

function! s:ShowLast() abort
  let l:file = s:state_dir . '/last_suggestion.txt'
  if !filereadable(l:file)
    echo 'claude-pair: no suggestion yet'
    return
  endif
  let l:lines = readfile(l:file)
  " reuse the previous suggestion window if it's still open
  let l:existing = bufwinnr('claude-pair://last')
  if l:existing > 0
    execute l:existing . 'wincmd w'
    setlocal modifiable
    silent %delete _
  else
    botright new
    setlocal buftype=nofile bufhidden=wipe noswapfile nobuflisted
    " suggestions are light markdown; fenced blocks get language highlighting
    if !exists('g:markdown_fenced_languages')
      let g:markdown_fenced_languages = ['python', 'fish', 'sh', 'vim']
    endif
    setlocal filetype=markdown
    setlocal conceallevel=2 nonumber norelativenumber signcolumn=no
    silent! file claude-pair://last
    nnoremap <silent> <buffer> q :close<CR>
  endif
  call setline(1, l:lines)
  execute 'resize' max([3, min([len(l:lines) + 1, 12])])
  setlocal nomodifiable
endfunction

command! ClaudeLast call s:PasteLast()
command! ClaudeLastShow call s:ShowLast()

nnoremap <silent> <Plug>(ClaudePairLast) :call <SID>PasteLast()<CR>
nnoremap <silent> <Plug>(ClaudePairShow) :call <SID>ShowLast()<CR>
if get(g:, 'claude_pair_default_mappings', 1)
  if !hasmapto('<Plug>(ClaudePairLast)') && empty(maparg('<Leader>cl', 'n'))
    nmap <Leader>cl <Plug>(ClaudePairLast)
  endif
  if !hasmapto('<Plug>(ClaudePairShow)') && empty(maparg('<Leader>cs', 'n'))
    nmap <Leader>cs <Plug>(ClaudePairShow)
  endif
endif
