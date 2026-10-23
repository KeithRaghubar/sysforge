# demo.tape -- the README terminal recording (docs/assets/demo.gif).
# Render with `make demo-gif`, which builds the demo environment this tape
# expects (SF_DEMO_HOME, SYSFORGE_CONFIG_DIR, SYSFORGE_STATE_DIR) first.

Output docs/assets/demo.gif

Set Shell "bash"
Set FontFamily "Fira Code"
Set FontSize 14
Set Width 1500
Set Height 720
Set Padding 24
Set Theme "GitHub Dark"
Set TypingSpeed 35ms

Hide
Type "export HOME=$SF_DEMO_HOME PS1='$ '; cd; clear" Enter
Sleep 500ms
Show

Sleep 500ms
Type "sysforge resolve mesa --show-flags" Sleep 400ms Enter
Sleep 5s
Type "sysforge update --dry-run --offline --explain-drift gamescope-nvidia" Sleep 400ms Enter
Sleep 7s
