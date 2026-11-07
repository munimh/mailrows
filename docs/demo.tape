# Terminal recording for the README. Render with:  vhs docs/demo.tape
# Needs vhs (https://github.com/charmbracelet/vhs) and mailrows on PATH.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1100
Set Height 980
Set Padding 20
Set TypingSpeed 45ms
Set Theme "Builtin Dark"

# start from an empty folder, off camera
Hide
Type "cd \"$(mktemp -d)\" && clear"
Enter
Sleep 500ms
Show

Type "mailrows init demo"
Sleep 400ms
Enter
Sleep 2s

# clear first so the whole preview (table, skipped row, sample email) fits on screen
Type "clear && cd demo && mailrows send --dry-run"
Sleep 400ms
Enter
Sleep 7s
