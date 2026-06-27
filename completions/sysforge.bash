# bash completion for sysforge
#
# Mirrors completions/_sysforge (zsh). Source of truth for the CLI surface is
# sysforge/cli.py.
#
# Installed by PKGBUILD to /usr/share/bash-completion/completions/sysforge.
# Requires the `bash-completion` package (declared as an optdep).

_sysforge() {
    local cur prev words cword
    if declare -F _init_completion >/dev/null; then
        _init_completion -n : 2>/dev/null
    else
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    fi

    local commands="build fetch update resolve doctor packages state run setup env log config"

    # Locate the top-level verb (first non-flag arg after `sysforge`) and an
    # optional subverb (first non-flag arg after the verb).
    local verb="" subverb=""
    local i
    for ((i=1; i<cword; i++)); do
        local w="${words[i]}"
        case "$w" in
            -*) ;;
            *)
                if [[ -z $verb ]]; then
                    verb="$w"
                elif [[ -z $subverb ]]; then
                    subverb="$w"
                fi
                ;;
        esac
    done

    if [[ $prev == "--color" ]]; then
        COMPREPLY=( $(compgen -W "auto always never" -- "$cur") )
        return 0
    fi

    if [[ -z $verb ]]; then
        if [[ $cur == -* ]]; then
            COMPREPLY=( $(compgen -W "-v --verbose --py-profile --py-profile-out --timings --color" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        fi
        return 0
    fi

    case "$verb" in
        build)       _sysforge_build       ;;
        fetch)       _sysforge_fetch       ;;
        update)      _sysforge_update      ;;
        resolve)     _sysforge_resolve     ;;
        doctor)      _sysforge_doctor      ;;
        packages)    _sysforge_packages    ;;
        state)       _sysforge_state       ;;
        run)         _sysforge_run         ;;
        setup)       _sysforge_setup       ;;
        env)         _sysforge_env         ;;
        config)      _sysforge_config      ;;
        completions) _sysforge_completions ;;
    esac
}

# Match a flag-with-value pair (e.g. `--profile-conf <TAB>`) on $prev and
# complete its argument. Returns 0 if handled.
_sysforge_flag_arg() {
    case "$prev" in
        --profile-conf|--packages|--pacman-conf)
            _filedir
            return 0
            ;;
        --state-dir|--log-dir|--config-dir)
            _filedir -d
            return 0
            ;;
        -m|--makepkg|--reason|--cc|--cxx)
            return 0
            ;;
        --ld)
            COMPREPLY=( $(compgen -W "lld mold ld bfd" -- "$cur") )
            return 0
            ;;
        --start-from)
            COMPREPLY=( $(compgen -W "reconfigure toolchain packages kernel" -- "$cur") )
            return 0
            ;;
        --compiler)
            COMPREPLY=( $(compgen -W "gcc llvm" -- "$cur") )
            return 0
            ;;
        --bootloader)
            COMPREPLY=( $(compgen -W "systemd-boot grub none" -- "$cur") )
            return 0
            ;;
        --base-config)
            COMPREPLY=( $(compgen -W "pkgbuild running" -- "$cur") )
            return 0
            ;;
        --source)
            COMPREPLY=( $(compgen -W "repo aur local" -- "$cur") )
            return 0
            ;;
        --pgo)
            COMPREPLY=( $(compgen -W "record use" -- "$cur") )
            return 0
            ;;
        --autofdo)
            COMPREPLY=( $(compgen -W "record capture use" -- "$cur") )
            return 0
            ;;
    esac
    return 1
}

_sysforge_pkg_names() {
    local pkgs
    if [[ -z $cur ]]; then
        pkgs=$(sysforge completions local 2>/dev/null)
    else
        pkgs=$(sysforge completions packages 2>/dev/null | grep -m 201 -- "^$cur")
    fi
    COMPREPLY=( $(compgen -W "$pkgs" -- "$cur") )
}

_sysforge_local_pkg_names() {
    local pkgs
    pkgs=$(sysforge completions local 2>/dev/null)
    COMPREPLY=( $(compgen -W "$pkgs" -- "$cur") )
}

_sysforge_state_names() {
    local pkgs
    pkgs=$(sysforge completions state 2>/dev/null)
    COMPREPLY=( $(compgen -W "$pkgs" -- "$cur") )
}

_sysforge_manifest_names() {
    local pkgs
    pkgs=$(sysforge completions manifest 2>/dev/null)
    COMPREPLY=( $(compgen -W "$pkgs" -- "$cur") )
}

_sysforge_installed_pkg_names() {
    local pkgs
    pkgs=$(pacman -Qq 2>/dev/null)
    COMPREPLY=( $(compgen -W "$pkgs" -- "$cur") )
}

_sysforge_build() {
    _sysforge_flag_arg && return
    local flags="-m --makepkg --interactive --profile-conf --cc --cxx --ld \
        --no-pkg-log --log-dir --persist-log --cache-report --abi-check \
        --no-update --cleansrc --cleansrc-force --no-llvm-preflight \
        --no-review --force --pgo --state-dir"
    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        _sysforge_pkg_names || _filedir
    fi
}

_sysforge_fetch() {
    _sysforge_flag_arg && return
    local flags="--no-update --cleansrc --cleansrc-force --no-llvm-preflight --profile-conf"
    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        _sysforge_pkg_names
    fi
}

_sysforge_update() {
    _sysforge_flag_arg && return
    local flags="--packages --dry-run --devel --offline --install-only --interactive \
        --no-cleanbuild --cleansrc --cleansrc-force --no-llvm-preflight \
        --review --no-review --no-toolchain-preflight --include-stage-owned \
        --explain-drift --rebuild-on-toolchain-drift --rebuild-on-flag-drift --rebuild-on-drift \
        -m --makepkg --state-dir --profile-conf --cache-report \
        --no-pkg-log --persist-log --log-dir"
    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        _sysforge_state_names
    fi
}

_sysforge_resolve() {
    _sysforge_flag_arg && return
    local flags="--show-flags --deps --profile-conf"
    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        _sysforge_local_pkg_names
    fi
}

_sysforge_doctor() {
    local flags="--graphics --hardware --toolchain --pacman --state --boot --services --audio --network \
        --all --repo --shallow -q --quiet -s --suggest --apply --no-confirm --dry-run"
    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        _sysforge_installed_pkg_names
    fi
}

_sysforge_packages() {
    _sysforge_flag_arg && return
    if [[ -z $subverb ]]; then
        if [[ $cur == -* ]]; then
            COMPREPLY=( $(compgen -W "--packages --orphans" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "list add add-group remove" -- "$cur") )
        fi
        return
    fi
    case "$subverb" in
        list)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--packages --orphans" -- "$cur") )
            ;;
        add)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--packages --source --enable-build-from-source --no-cache --reason" -- "$cur") )
            else
                _sysforge_pkg_names
            fi
            ;;
        add-group)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--packages" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "gnome kde xfce mate cinnamon lxqt budgie cosmic" -- "$cur") )
            fi
            ;;
        remove)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--packages" -- "$cur") )
            else
                _sysforge_manifest_names
            fi
            ;;
    esac
}

_sysforge_state() {
    _sysforge_flag_arg && return
    if [[ -z $subverb ]]; then
        COMPREPLY=( $(compgen -W "list repair orphans failed forget" -- "$cur") )
        return
    fi
    case "$subverb" in
        list)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--state-dir --no-pager" -- "$cur") )
            ;;
        orphans)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--prune --no-confirm --no-pager" -- "$cur") )
            ;;
        repair)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--state-dir --dry-run" -- "$cur") )
            ;;
        failed)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--state-dir --no-pager --clear --clear-all" -- "$cur") )
            ;;
        forget)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--state-dir" -- "$cur") )
            else
                _sysforge_state_names
            fi
            ;;
    esac
}

_sysforge_run() {
    _sysforge_flag_arg && return
    if [[ -z $subverb ]]; then
        COMPREPLY=( $(compgen -W "pipeline hardware reconfigure toolchain packages kernel" -- "$cur") )
        return
    fi
    case "$subverb" in
        pipeline)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "\
                --resume --start-from --force-retry --dry-run --packages \
                --state-dir --profile-conf --no-unified-log --no-pkg-logs \
                --log-dir --purge-log --persist-log --cache-report --abi-check \
                --no-update" -- "$cur") )
            ;;
        hardware)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--dry-run --state-dir" -- "$cur") )
            ;;
        reconfigure)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--dry-run --packages --state-dir" -- "$cur") )
            ;;
        toolchain)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "\
                --dry-run --no-update -m --makepkg --persist-log --cache-report \
                --abi-check --state-dir --rebuild-profdata --reuse-built --auto-pgo \
                --allow-dirty-llvm --allow-version-skew --skip-build-space-check \
                --rebuild-soname-consumers --cleansrc --cleansrc-force" -- "$cur") )
            ;;
        packages)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "\
                --packages --dry-run --force-retry --no-update --no-pkg-logs \
                --persist-log --log-dir --cache-report --abi-check --state-dir \
                --profile-conf" -- "$cur") )
            ;;
        kernel)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "\
                --dry-run --no-update --cleansrc --cleansrc-force \
                --non-interactive --compiler --bootloader --base-config \
                --allow-no-fallback --skip-boot-audit \
                --headers --no-headers --docs --no-docs \
                --autofdo --propeller --no-pkg-logs \
                --persist-log --log-dir --cache-report --abi-check \
                --state-dir --profile-conf" -- "$cur") )
            ;;
    esac
}

_sysforge_setup() {
    _sysforge_flag_arg && return
    [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--pacman-conf" -- "$cur") )
}

_sysforge_env() {
    :
}

_sysforge_config() {
    _sysforge_flag_arg && return
    if [[ -z $subverb ]]; then
        COMPREPLY=( $(compgen -W "merge" -- "$cur") )
        return
    fi
    case "$subverb" in
        merge)
            [[ $cur == -* ]] && COMPREPLY=( $(compgen -W "--config-dir --list --dry-run --no-pager" -- "$cur") )
            ;;
    esac
}

# Internal scaffolding subcommand consumed by this completion script itself
# (and the zsh equivalent). Hidden from `sysforge --help` (argparse.SUPPRESS)
# but exposed here so `sysforge completions <TAB>` completes the resource arg.
_sysforge_completions() {
    if [[ -z $subverb ]]; then
        COMPREPLY=( $(compgen -W "packages manifest local state makepkg-flags" -- "$cur") )
    fi
}

complete -F _sysforge sysforge
