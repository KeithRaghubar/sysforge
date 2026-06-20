## Graphics Stack Build Order

Build in this order to satisfy dependencies correctly:

1. **Stage 1 — LLVM**
   * PGO (64-bit): `llvm`, `llvm-libs`, `clang`, `lld`
   * Non-PGO (64-bit): `polly`, `compiler-rt`, `openmp`, `spirv-llvm-translator`
   * Non-PGO (lib32): `lib32-llvm`, `lib32-llvm-libs`, `lib32-clang`, `lib32-spirv-llvm-translator`
2. `vulkan-headers-git`
3. `vulkan-icd-loader-git`, `lib32-vulkan-icd-loader`
4. `mesa-git`, `lib32-mesa-git`
5. `egl-wayland`, `lib32-egl-wayland`, `xwayland-git`
6. `libinput-git`
7. COSMIC git packages
8. `xwayland-satellite`

**Reduced LLVM targets must keep `AMDGPU`.** When the toolchain stage rebuilds Stage-1 `llvm`/`llvm-libs` with a reduced `LLVM_TARGETS_TO_BUILD` (see §Hardware detection → *LLVM target derivation*), the set **must** include `AMDGPU` even on nvidia/intel-only hosts. Stage-4 `mesa` links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**; a system `libLLVM` missing them fails to load with `undefined symbol: LLVMInitializeAMDGPU…`, taking down every EGL/GL client (`cosmic-comp`, the greeter — the whole desktop), with healthy kernel/KMS still presenting as a black screen. `hardware.derive_llvm_targets` guarantees this via `_SYSTEM_LIBLLVM_CONSUMER_TARGETS`, and `llvm_targets.resolve_or_detect_llvm_targets` re-asserts it at resolution time so a stale/edited `hardware_profile.toml` can't drop it (see §Hardware detection → *LLVM target derivation*).

**Defense in depth (the desktop must never black-screen from a toolchain rebuild).** Three layers back the rule above: (1) **prevent** — the resolution-time AMDGPU baseline; (2) **catch pre-install** — toolchain **Gate 2** (`toolchain_safety.check_system_consumer_symbols`) `ldd -r`-diffs the freshly-built `libLLVM` against installed mesa consumers (`libgallium`/DRI/Vulkan) and aborts *before* `pacman -U` if any `LLVMInitialize*@LLVM_x.y` symbol they import would go missing — the live graphics stack is untouched; (3) **verify post-install** — **Gate 3** re-runs the diff against the now-installed libLLVM and triggers the snapshot auto-rollback on a miss (see §`pipeline-layer` → toolchain gates). Separately, `sysforge doctor --graphics` surfaces the same fact (`graphics_probe._check_mesa_llvm_symbols` → `check_installed_consumer_symbols`) so a system already in this state self-diagnoses in one line instead of presenting only as a black screen.

---

