# repo-build.tape -- the getting-started guide's recording: a package from the
# Arch repos rebuilt from its own PKGBUILD with the default profile.
# Render with `make demo-gif`, which starts the container this tape drives.

Output docs/assets/repo-build.gif
Require podman

Set Shell "bash"
Set FontFamily "Fira Code"
Set FontSize 15
Set Width 1200
Set Height 640
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
Type "pacman -Si tree | grep -E '^(Name|Version|Groups)'" Sleep 300ms Enter
Sleep 2500ms
Type "sysforge build tree -si" Sleep 400ms Enter
# A repo package is only built from source once you opt in; the answer is saved.
Wait+Screen /build from source\?/
Sleep 1500ms
Type "y" Enter
Wait+Line /^\$ ?$/
Sleep 1500ms
Type "pacman -Qi tree | grep -E '^(Name|Version|Groups)'" Sleep 300ms Enter
Sleep 4s
