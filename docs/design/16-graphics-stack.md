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

**Reduced LLVM targets must keep `AMDGPU`.** When the toolchain stage rebuilds Stage-1 `llvm`/`llvm-libs` with a reduced `LLVM_TARGETS_TO_BUILD` (see §Hardware detection → *LLVM target derivation*), the set **must** include `AMDGPU` even on nvidia/intel-only hosts. Stage-4 `mesa` links the `AMDGPU` (radeonsi) and host-CPU (llvmpipe) target-init symbols from `libgallium` **unconditionally**; a system `libLLVM` missing them fails to load with `undefined symbol: LLVMInitializeAMDGPU…`, taking down every EGL/GL client (`cosmic-comp`, the greeter — the whole desktop), with healthy kernel/KMS still presenting as a black screen. `hardware.derive_llvm_targets` guarantees this via `SYSTEM_LIBLLVM_CONSUMER_TARGETS`, and `llvm_targets.resolve_or_detect_llvm_targets` re-asserts it at resolution time so a stale/edited `hardware_profile.toml` can't drop it (see §Hardware detection → *LLVM target derivation*).

**Defense in depth (the desktop must never black-screen from a toolchain rebuild).** Three layers back the rule above: (1) **prevent** — the resolution-time AMDGPU baseline; (2) **catch pre-install** — toolchain **Gate 2** (`toolchain_safety.check_system_consumer_symbols`) `ldd -r`-diffs the freshly-built `libLLVM` against installed mesa consumers (`libgallium`/DRI/Vulkan) and aborts *before* `pacman -U` if any `LLVMInitialize*@LLVM_x.y` symbol they import would go missing — the live graphics stack is untouched; (3) **verify post-install** — **Gate 3** re-runs the diff against the now-installed libLLVM and triggers the snapshot auto-rollback on a miss (see §`pipeline-layer` → toolchain gates). Separately, `sysforge doctor --graphics` surfaces the same fact (`graphics_probe._check_mesa_llvm_symbols` → `check_installed_consumer_symbols`) so a system already in this state self-diagnoses in one line instead of presenting only as a black screen.

**Two stranding classes — only one is a hard block.** The Gate-2/3 consumer-symbol diff (`_diff_consumers_against_libllvm`) splits its findings by whether any missing symbol is an `LLVMInitialize*` target-init entry point:

- **Target-init drop (unhealable).** A dropped LLVM backend (e.g. a reduced `LLVM_TARGETS_TO_BUILD` without `AMDGPU`). The symbol would not exist anywhere — rebuilding the consumer cannot recover it. Gate 2 hard-aborts before install; the AMDGPU baseline is the real fix. **Unchanged behaviour.**
- **`std::` re-export drop (healable).** A drop of *only* non-target-init `LLVM_*`-versioned symbols. These are libstdc++ `std::__cxx11::basic_string` methods that LLVM's `global: *` version script globs into the `LLVM_<ver>` node as out-of-line weak copies. The official `llvm-libs` exports them; mesa links them as `…@LLVM_<ver>`. A PGO (`-fprofile-use`) `libLLVM` **inlines those weak copies away**, so the optimized lib — *at the same soname* — no longer exports them and mesa is stranded. This is healable: rebuilding mesa against the new `libLLVM` re-links the symbols to libstdc++ (`@GLIBCXX_*`), exactly as a distro does on an llvm rebump.

For the healable class Gate 2 does **not** abort. It captures the installed libLLVM consumers via `toolchain_safety.libllvm_abi_consumers` (the reverse-dep `%DEPENDS%` walk factored out of `assess_libllvm_soname_impact` into `libllvm_soname_consumers`, here **not** gated on a soname change) and rebuilds them after Gate 3 through the same `_rebuild_soname_consumers` path used for a soname bump — gated by the existing `rebuild_soname_consumers` mode (`prompt` default | `auto` | `off`). Gate 3 tolerates the healable miss (mesa is not rebuilt until *after* Gate 3, so a rollback there would revert the very libLLVM the rebuild is about to make coherent); only target-init misses still trip the auto-rollback. The system self-stabilises: once mesa links the optimized libLLVM it stops importing the `…@LLVM_<ver>` `std::` symbols, so a subsequent `run toolchain` sees no stranding and queues no rebuild. Note `assess_libllvm_soname_impact` still short-circuits on a same-version (`old_mm == target_mm`) refresh — the std:: drift is owned by this Gate-2/3 + `libllvm_abi_consumers` path, not the soname-bump gate.

---

