# SysForge Design Document

SysForge is an AUR helper for Arch Linux with compiler optimization as a first-class concern. It manages AUR and custom package builds using rule-based compiler flag profiles, tracks build state for update detection, and automates the full build lifecycle — from fetching PKGBUILDs to installing profiled packages. Pacman owns the package database; SysForge owns the build configuration layer above it.

Current release is **<!--version-->v1.2.0<!--/version-->**. v0.1.0 shipped the profiled AUR helper surface (install, update, and manage AUR and custom packages with system-tuned profiled builds); v0.2.0 added VM tooling and install-path fixes on top; v1.0 rounds out the system-bootstrapper milestone — the full bootstrap pipeline (stages 1–4: partition, base install, hardware detection, configure) is implemented and a fresh Arch install is automated from the ISO. See the [Release Plan](#release-plan) for the shipped-vs-remaining breakdown.

---

## Table of Contents

<!--TOC-->

---

