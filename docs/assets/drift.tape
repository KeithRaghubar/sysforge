# drift.tape -- the drift guide's recording: after a profile change, update
# finds the package built with the old flags and rebuilds it.
# Render with `make demo-gif`, which starts the container this tape drives.

Output docs/assets/drift.gif
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

# Off camera: build a package with the shipped default profile (standard),
# then switch the default to optimized.
Hide
Type "podman exec -it -u demo -w /home/demo $DEMO_CONTAINER bash" Enter
Sleep 1s
Type "sysforge build cbonsai -si" Enter
Wait+Screen /\[a\]ccept/
Type "a"
Wait+Line /\$ ?$/
Type `sudo sed -i 's/^profile = "standard"/profile = "optimized"/' /etc/sysforge/profiles.toml; PS1='$ '; clear` Enter
Sleep 1s
Show

Sleep 500ms
Type `grep '^profile' /etc/sysforge/profiles.toml   # was "standard" when cbonsai was built` Sleep 300ms Enter
Sleep 2500ms
Type "sysforge update --dry-run --explain-drift" Sleep 400ms Enter
Wait+Line /^\$ ?$/
Sleep 3500ms
Type "sysforge update --rebuild-on-flag-drift --yes" Sleep 400ms Enter
Wait+Line /^\$ ?$/
Sleep 4s
