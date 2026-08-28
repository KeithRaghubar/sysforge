# SysForge Design Document

SysForge is an Arch Linux build and maintenance suite with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

SysForge manages the profiled AUR-helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds) and a full bootstrap pipeline (stages 1–3: install via archinstall, hardware detection, configure) that automates a fresh Arch install from the ISO. Current release is **<!--version-->v3.2.0<!--/version-->**; per-release changes are recorded in `docs/release-notes/`.

---

## Table of Contents

<!--TOC-->

---

