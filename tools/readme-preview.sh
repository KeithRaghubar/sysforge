#!/usr/bin/env bash
# readme-preview.sh -- render a Markdown file the way GitHub does and open it.
#
# Usage: readme-preview.sh [FILE]   (default README.md)
#
# GitHub's own renderer (`gh api markdown`, GFM with this repo as context) turns
# the file into HTML, wrapped in GitHub's stylesheet at the repo page's column
# width. A <base> tag points at the file's directory in this checkout, so images
# and relative links (docs/assets/demo.gif) load from the working tree. Nothing
# is pushed or published; the file's text is only sent to GitHub to render.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
file=${1:-README.md}
case $file in
    /*) path=$file ;;
    *)  path=$repo/$file ;;
esac
if [[ ! -f $path ]]; then
    echo "readme-preview: $file not found" >&2
    exit 1
fi
if ! command -v gh >/dev/null; then
    echo "readme-preview: gh is not installed (sudo pacman -S github-cli)" >&2
    exit 1
fi

out=${TMPDIR:-/tmp}/sysforge-preview-$(basename "$path" .md).html
base=$(cd "$(dirname "$path")" && pwd)
{
    cat <<EOF
<!doctype html>
<meta charset="utf-8">
<title>$(basename "$path") preview</title>
<base href="file://$base/">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown.min.css">
<style>
body { margin: 0; background: #fff; }
@media (prefers-color-scheme: dark) { body { background: #0d1117; } }
.markdown-body { box-sizing: border-box; max-width: 1012px; margin: 0 auto; padding: 45px; }
</style>
<article class="markdown-body">
EOF
    gh api -X POST markdown -f mode=gfm -f context=KeithRaghubar/sysforge -F text=@"$path"
    echo '</article>'
} > "$out"

echo "readme-preview: wrote $out"
if command -v xdg-open >/dev/null && [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then
    xdg-open "$out" >/dev/null 2>&1 &
fi
