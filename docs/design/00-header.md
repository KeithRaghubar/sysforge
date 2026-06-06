# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

Current release is **<!--version-->v1.2.0<!--/version-->**. v0.1.0 shipped the profiled AUR helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds); v0.2.0 added VM tooling and install-path fixes on top; v1.0 rounds out the system-bootstrapper milestone — the full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) is implemented and a fresh Arch install is automated from the ISO. See the [Release Plan](#release-plan) for the shipped-vs-remaining breakdown.

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Distribution Model](#distribution-model)
3. [Architecture Overview](#architecture-overview)
4. [Directory Structure](#directory-structure)
5. [Package Manifest](#package-manifest)
6. [Config Layer](#config-layer)
7. [Pipeline Layer](#pipeline-layer)
8. [CLI Verb Framework](#cli-verb-framework)
9. [Primitives Layer](#primitives-layer)
10. [Flag Profile System](#flag-profile-system)
11. [Makepkg Wrapper](#makepkg-wrapper)
12. [Logging](#logging)
13. [Man Pages](#man-pages)
14. [Hardware Detection](#hardware-detection)
15. [Cache Management](#cache-management)
16. [Graphics Stack Build Order](#graphics-stack-build-order)
17. [Release Plan](#release-plan)
18. [Re-converge](#re-converge)
19. [Known Gaps](#known-gaps)
20. [V1.x Roadmap](#v1x-roadmap)
21. [V2 Roadmap](#v2-roadmap)

---

