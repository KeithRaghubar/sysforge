# quickstart.tape -- the README recording: Quick start steps 4 and 5 on a fresh
# system, an AUR package built with the default profile and then maintained.
# Render with `make demo-gif`, which starts the container this tape drives
# ($DEMO_CONTAINER, set up to the end of Quick start step 3).

Output docs/assets/quickstart.gif
Require podman

Set Shell "bash"
Set FontFamily "Fira Code"
Set FontSize 15
Set Width 1200
Set Height 720
Set Padding 24
Set Theme "GitHub Dark"
Set TypingSpeed 35ms
Set Framerate 20
Set WaitTimeout 3m

Hide
Type "podman exec -it -u demo -w /home/demo $DEMO_CONTAINER bash" Enter
Sleep 1s
Type "PS1='$ '; clear" Enter
Sleep 500ms
Show

Sleep 500ms
Type "sysforge build cbonsai -si" Sleep 400ms Enter
# The first build of an AUR package asks for a review of its PKGBUILD.
Wait+Screen /\[a\]ccept/
Sleep 1500ms
Type "v"
Sleep 3s
Type "q"
Wait+Screen /\[a\]ccept/
Sleep 1s
Type "a"
Wait+Line /^\$ ?$/
Sleep 1500ms
Type "cbonsai -p" Sleep 300ms Enter
Sleep 3s
Type "sysforge update" Sleep 300ms Enter
Wait+Line /^\$ ?$/
Sleep 4s
