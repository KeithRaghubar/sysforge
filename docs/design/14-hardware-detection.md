## Hardware Detection

Pipeline stage 3. Probes the running system via `/proc/cpuinfo` and `lspci`, emits `hardware_profile.toml` to `state_dir`. The file feeds kconfig automation (kernel stage) and is shown in the reconfigure config review.

**Detections and kconfig output:**

| Hardware | Detection | kconfig |
|---|---|---|
| AMD Zen 3 (family 25, model 33/80/68/24) | `/proc/cpuinfo` | `CONFIG_MZEN3 = y` |
| AMD Zen 4 (family 25, model 97/116/117) | `/proc/cpuinfo` | `CONFIG_MZEN4 = y` |
| AMD Zen 5 (family 26) | `/proc/cpuinfo` | `CONFIG_MZEN5 = y` |
| AMD CPU (family ≥ 25) | `/proc/cpuinfo` | `CONFIG_X86_AMD_PSTATE = y` |
| NVIDIA GPU | `lspci` | `CONFIG_DRM_NOUVEAU = n` |
| NVMe storage | `lspci` | `CONFIG_BLK_DEV_NVME = y` |

Unknown AMD CPU models get `CONFIG_X86_AMD_PSTATE` but no `CONFIG_MZEN*` entry — the kernel defaults to `CONFIG_GENERIC_CPU`.

**LLVM target derivation.** The hardware stage also writes `host_arch` (from `uname -m`) and an autodetected `llvm_targets` list — CPU backend from arch (`x86_64`→`X86`, `aarch64`→`AArch64`, `armv7l`→`ARM`, `riscv64`→`RISCV`, `ppc64le`→`PowerPC`) plus GPU backends from `gpu_vendors` (`amd`→`AMDGPU`, `nvidia`→`NVPTX`; `intel` contributes nothing because the Mesa Intel drivers don't depend on an LLVM backend). **Plus a mandatory `AMDGPU` baseline (`_SYSTEM_LIBLLVM_CONSUMER_TARGETS`) on every recognised arch — even nvidia/intel-only hosts:** the *system* `mesa` package links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**, so a rebuilt system `llvm-libs` that dropped `AMDGPU` leaves mesa with `undefined symbol: LLVMInitializeAMDGPU…` and bricks every EGL/GL consumer (the whole desktop). An unrecognised arch yields an empty list — "no filtering", i.e. upstream builds all targets, which is also safe for mesa. Consumed by `pkgbuild_patcher.patch_llvm_targets` when building any LLVM-toolchain package.

**The baseline is enforced at *resolution* time, not only at derivation.** `derive_llvm_targets` bakes `AMDGPU` into a freshly-derived list, but the actual build resolves `LLVM_TARGETS_TO_BUILD` from `hardware_profile.toml` (or an explicit `toolchain.toml [llvm] targets`) via `llvm_targets.resolve_or_detect_llvm_targets` — both of which *bypass* derivation. A profile cached before the baseline existed, or one a user hand-edited, would otherwise silently reintroduce a brick. So `resolve_llvm_targets`/`resolve_or_detect_llvm_targets` re-apply `_SYSTEM_LIBLLVM_CONSUMER_TARGETS` (via `_ensure_system_consumer_targets`) to **any** non-None, non-empty resolved set, from any source. The single opt-out is `[llvm] targets = []` ("build all", which already includes `AMDGPU`), which resolves to `None` and skips the enforcement. This is the layer the bricked-desktop regression slipped through: the fix had lived only in derivation while the build read the cached file.

**Mesa driver derivation (the meson analogue).** The hardware stage also writes `mesa_gallium_drivers` / `mesa_vulkan_drivers` from the same `gpu_vendors` (`derive_mesa_drivers`): `amd`→`radeonsi`/`amd`, `intel`→`iris,crocus`/`intel,intel_hasvk`, `nvidia`→`nouveau`/`nouveau`. These trim mesa's `-D gallium-drivers=all` / `-D vulkan-drivers=<every-driver>` (every ARM-SoC/mobile GPU mesa ships) down to what the host runs — a real build-time win when sysforge source-builds mesa. The invariant is the *inverse* of the LLVM `AMDGPU` one: where that guards against reducing *too little*, mesa's mandatory software baseline (`_MESA_MANDATORY_GALLIUM` = `llvmpipe`/`softpipe`/`zink`, `_MESA_MANDATORY_VULKAN` = `swrast`/lavapipe) guards against reducing *too much* — a build with no software renderer bricks headless/VM/GPU-reset-recovery sessions. The baseline rides every derived/resolved set, even a no-GPU host (which yields baseline-only). **Unlike LLVM filtering, mesa filtering is opt-in** (`[mesa] filter_drivers = true` in `sysforge.toml`, default off); resolution (`mesa_drivers.resolve_or_detect_mesa_drivers`) and baseline enforcement (`_ensure_mesa_software_baseline`) mirror the LLVM path, and a gallium reduction also intersects `gallium-rusticl-enable-drivers` with the built set (rusticl drivers must be a subset). Consumed by `pkgbuild_patcher.patch_mesa_drivers` (gated by `profile.is_mesa_pkgbase`) when building any mesa-family package; lib32-mesa **is** filtered (vendor- not arch-coupled, unlike lib32-llvm).

**`hardware_profile.toml` layout:**
```toml
[hardware]
cpu_vendor  = "AuthenticAMD"
cpu_family  = 25
cpu_model   = 33
host_arch   = "x86_64"
gpu_vendors = ["nvidia"]
llvm_targets = ["X86", "NVPTX", "AMDGPU"]  # AMDGPU always present (system mesa)
mesa_gallium_drivers = ["nouveau", "llvmpipe", "softpipe", "zink"]  # + software baseline
mesa_vulkan_drivers  = ["nouveau", "swrast"]                        # swrast = lavapipe baseline
nvme        = true

[kconfig]
CONFIG_MZEN3          = "y"
CONFIG_X86_AMD_PSTATE = "y"
CONFIG_DRM_NOUVEAU    = "n"
CONFIG_BLK_DEV_NVME   = "y"
# … plus arch-disable `=n` entries for every non-host kconfig domain
CONFIG_ARM64          = "n"
CONFIG_ARCH_QCOM      = "n"
# (and the rest of _ARCH_OWNED_KCONFIG minus the host's own domain)

[kconfig_devices]
# device-driven modular drivers for present devices, all "m" — see
# §Device-driven kconfig below
CONFIG_SND_HDA_INTEL  = "m"
CONFIG_IGC            = "m"
```

Written atomically (write-then-rename) to `<state_dir>/hardware_profile.toml`. The file has four readers:

- **`pipeline/stages/kernel.py`** — `_load_hardware_kconfig()` consumes `[kconfig]` and `[kconfig_devices]`; entries flow into the `sysforge.config` fragment merged into `.config` via `merge_config.sh` (precedence: manual `[[kconfig]]` > `[kconfig]` > `[kconfig_devices]`; the device table is gated by `kernel.toml device_kconfig`, default true). Absence is non-fatal (entries skipped with an INFO log).
- **`primitives/llvm_targets.py`** — `_read_hardware_targets()` consumes `[hardware] llvm_targets`; resolves the `LLVM_TARGETS_TO_BUILD` cmake arg injected by `pkgbuild_patcher.patch_llvm_targets`.
- **`primitives/mesa_drivers.py`** — `_read_hardware_drivers()` consumes `[hardware] mesa_gallium_drivers` / `mesa_vulkan_drivers`; resolves (opt-in, gated by `sysforge.toml [mesa] filter_drivers`) the `-D gallium-drivers=` / `-D vulkan-drivers=` meson options rewritten by `pkgbuild_patcher.patch_mesa_drivers`.
- **`pipeline/stages/reconfigure.py`** — surfaces the file in the pre-build config review so the user can hand-edit before kernel build.
- **`commands/doctor.py`** — consumes `[hardware] gpu_vendors` to scope the `doctor --graphics` health checks.

### Architecture-aware kconfig disable

In addition to the positive `=y` enables above, the hardware stage emits an `=n` line for every CONFIG_* key owned by a kernel architecture domain that is **not** the host's domain. The data lives in two module-level constants in `pipeline/stages/hardware.py`:

- `_ARCH_OWNED_KCONFIG: dict[str, frozenset[str]]` — `domain → set of CONFIG_* keys that only make sense when the kernel is targeting that domain`. Domains: `x86`, `arm` (32-bit), `arm64`, `riscv`, `powerpc`, `mips`, `sparc`, `loongarch`. Keys are **curated, not exhaustive** — top-level architecture umbrellas (`CONFIG_X86`, `CONFIG_ARM64`, …) plus the major SoC family umbrellas under `arm64` (`CONFIG_ARCH_QCOM`, `CONFIG_ARCH_TEGRA`, `CONFIG_ARCH_ROCKCHIP`, etc.). The Kconfig system itself gates most SoC drivers via `depends on ARCH_<vendor>`, so disabling the umbrella culls the subtree from `make nconfig` automatically.
- `_HOST_ARCH_TO_KCONFIG_DOMAIN: dict[str, str]` — `uname -m → domain`. Covers `x86_64`/`i686`/`i386` → `x86`, `aarch64` → `arm64`, `armv7l`/`armv6l` → `arm`, `riscv64`/`riscv32` → `riscv`, `ppc64le`/`ppc64`/`ppc` → `powerpc`, `mips`/`mips64` → `mips`, `sparc`/`sparc64` → `sparc`, `loongarch64` → `loongarch`.

`_arch_disable_kconfig(host_arch)` resolves the host domain, then iterates every *other* domain in the registry and emits `{CONFIG_X: "n"}`. Keys appearing in the host's own domain set are filtered out as a defensive guard (no clobber if a future kconfig key gains a presence in multiple domains). Unknown `host_arch` returns an empty dict and logs a WARN.

The `=n` entries land in the same `[kconfig]` table as the existing `=y` enables, so the kernel stage's existing merge path — `merged = {**device_kconfig, **hw_kconfig, **manual_kconfig}` — applies unchanged. A user cross-compiling or otherwise wanting an arch-disabled key re-enabled puts an explicit `[[kconfig]] option = "CONFIG_ARM64" value = "y"` in `kernel.toml`; the existing manual-override-wins-with-WARN behaviour in `_write_kconfig_fragment` extends to arch-disable entries.

### Device-driven kconfig (`[kconfig_devices]`)

The scalar `[kconfig]` heuristics cover CPU/GPU/NVMe; `[kconfig_devices]` covers everything else that is physically present. The stage takes the union of all enumerated devices' `suggested_kconfig` (see `device_probe.py` — modalias → expected module → `CONFIG_*`), subtracts any symbol the heuristic `[kconfig]` table already owns (so e.g. a nouveau-bound NVIDIA GPU can't re-enable the heuristic's `CONFIG_DRM_NOUVEAU = "n"`), and emits the rest as `"m"` — modular drivers don't load unless the hardware is present, so this is the safe default for device coverage.

The module→`CONFIG_*` resolution is two-layered: `device_probe`'s curated `_MODULE_TO_KCONFIG` table (vetted, always wins) plus the **kbuild map cache** (`<state_dir>/kbuild_module_map.json`, see §`kbuild_map.py`). The cache is harvested by the kernel stage's Gate 2 from the just-built source tree — the resolved `.config`'s parent is the version-exact tree, the only reliable place the kbuild Makefiles exist on disk (installed headers don't ship the nested driver Makefiles). The loop is self-improving: the first kernel build runs with curated-only coverage, Gate 2 caches the full tree-derived map, and every later hardware-stage run / fragment write resolves near-totally. The fold is consumed by the kernel stage's fragment merge at the lowest precedence (manual > hardware > device) and can be disabled wholesale with `kernel.toml device_kconfig = false`.

### Tested hardware scope

Design ambition is broad (every kconfig domain in the registry, every CPU/GPU brand the detection code recognises), but real-world validation is currently narrow. This section documents which paths have actually been exercised so users on untested hardware understand where they are taking implemented-but-unvalidated code paths.

**Tested on real iron** (the reference dev box):
- Host arch: `x86_64`
- CPU: AMD Ryzen 7 5800X3D — `AuthenticAMD` family 25 model 33 (Zen 3 / Vermeer)
- GPU: NVIDIA RTX 5070 (via `nvidia-open-dkms`)
- Storage: NVMe
- Distro: Arch Linux, kernel: custom `linux-custom` PKGBUILD

**Tested in VM** (`make vm-iso` → `make vm-install`):
- Host arch: `x86_64` (qemu/KVM guest)
- CPU: emulated/passthrough (typically host-passthrough)
- GPU: virtio (`gpu_vendors` likely empty or `["other"]`)
- Storage: virtio-blk or NVMe depending on VM config

**Implemented but never exercised against real hardware:**
- `host_arch ∈ {aarch64, armv7l, armv6l, riscv64, riscv32, ppc64le, ppc64, ppc, mips, mips64, sparc, sparc64, loongarch64}` — registry entries exist and are unit-tested, but no kernel has been built on any of these.
- Intel CPUs (`GenuineIntel`) — code path falls through to `CONFIG_GENERIC_CPU`, no Intel-specific CPU kconfig mapping exists.
- AMD CPUs older than Zen 3 — same fallback.
- AMD Zen 4 / Zen 5 — kconfig keys exist in `_AMD_CPU_KCONFIG` but the dev box predates them.
- Pure-AMD or pure-Intel GPU systems — Nvidia is the only GPU detection path exercised end-to-end.
- Non-NVMe storage (SATA, eMMC) — detection works (`_has_nvme` returns False), but no downstream `CONFIG_*` adjustment fires.

When a curated `=n` over-culls on an untested arch, the escape hatch is `kernel.toml [[kconfig]]` — adding the key back with `value = "y"` overrides the hardware-emitted disable per the existing merge semantics.

---

