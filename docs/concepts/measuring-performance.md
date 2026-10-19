# Measuring Performance Honestly

Every optimization on these pages promises a speed-up, and the only way to know whether you
got one is to measure. Measuring is easy to get wrong: a single run, timed once before and once
after, can show a 5% gain or loss that is pure noise. This page covers what makes timings
vary, how to compare two builds fairly, and the basics of `perf`.

The [glossary](glossary.md) defines the terms these pages share.

---

## Timings vary from run to run

Run the same command ten times and you get ten different times. The spread comes from things
that have nothing to do with the code being measured:

- **CPU frequency.** Modern CPUs boost their clock while they are cool and lightly loaded, and
  slow down as they heat up. The first run after an idle period often runs at a higher clock
  than the tenth.
- **Other work on the machine.** A browser tab, an indexing service or a package download
  competes for CPU, memory bandwidth and cache.
- **Caches.** The first run reads files from disk; later runs find them in the page cache in
  memory. The same applies on a smaller scale to CPU caches and branch predictors.
- **Memory layout.** Address-space randomization places code and data at different addresses
  on each run, which changes cache and branch-predictor collisions slightly. Even the size of
  the environment variables can shift the stack and change a timing.

None of this can be removed entirely, so a measurement has to show how big the variation is,
not just one number.

---

## Warm and cold

Decide which situation you are measuring, and keep it the same for every run.

- **Warm**: files, libraries and data are already in memory. This is what repeated use looks
  like, and it isolates CPU work. Do one or more unmeasured warm-up runs first.
- **Cold**: nothing is cached, as after a reboot. This is what first use looks like, and it
  includes disk speed. Drop the page cache before each run with
  `sync; echo 3 | sudo tee /proc/sys/vm/drop_caches`.

Mixing them, for example a cold run of the old build against a warm run of the new one, makes
the comparison meaningless.

---

## Repeat, and look at the spread

Run each case several times, and report the **median** (the middle value, which one unusual
run cannot drag around) together with the **spread**, such as the minimum and maximum or the
range covering most runs. Then compare:

- If the two ranges overlap heavily, you have not shown a difference, whatever the medians say.
- If they are clearly apart, the difference is probably real. More runs make a small difference
  easier to tell apart from noise.
- The smaller the effect you are looking for, the more runs you need. A 1% gain hidden in 3%
  run-to-run noise needs many runs to show at all.

**Interleave the runs.** Running all of build A, then all of build B, lets anything that
changes over time (temperature, a background job starting) look like a difference between
them. Alternating A, B, A, B spreads that drift across both.

---

## Hold everything else fixed

A before/after comparison only measures the change you made if nothing else changed between
the two. Keep the same:

- **Input**: the same files, the same workload, the same command-line options.
- **Machine state**: the same CPU governor or power profile, the same background programs,
  on mains power for a laptop, and the same cold or warm state.
- **Everything except the one change**: when comparing two compiler builds, compile the same
  code with the same flags. When comparing two builds of a library, run the same program
  against each. If the versions differ as well as the optimization, the result mixes upstream
  changes with your change and cannot separate them.

For less noise on a dedicated measurement, the [LLVM benchmarking tips](https://llvm.org/docs/Benchmarking.html)
suggest disabling address-space randomization for the measured program (`setarch -R`), pinning
it to fixed CPU cores (`taskset`), and disabling turbo boost and SMT. These make the numbers
steadier, but also less like everyday use, so they suit comparing two builds better than
predicting real-world speed.

A simple tool that does much of this for you is [hyperfine](https://github.com/sharkdp/hyperfine):
it runs warm-ups, repeats each command, reports mean, spread and outliers, and compares
commands against each other.

---

## perf basics

`perf` (package `perf`) reads the CPU's performance counters, and tells you not just how long
something took but why.

**Counting.** `perf stat` runs a command and prints totals:

```sh
perf stat -r 10 -- ./program input     # 10 runs; prints the mean with ± variation
perf stat -e cycles,instructions,branch-misses,L1-icache-load-misses -- ./program input
```

Useful numbers to compare between builds:

- **Instructions per cycle (IPC)**: how much work the CPU gets done per clock. A layout
  optimization often shows up as higher IPC for the same instruction count.
- **Branch misses** and **instruction-cache misses**: the costs that profile-guided and
  post-link optimization try to reduce.
- **Task clock and wall time**: the CPU time used versus the elapsed time.

Counters such as instructions and branches vary much less from run to run than elapsed time,
so they can show a change that timing alone cannot. But they are not the goal in themselves:
fewer cache misses only matter if the program gets faster.

**Sampling.** `perf record` samples where the program spends its time, and `perf report` shows
the result by function:

```sh
perf record -g -- ./program input      # -g also records call stacks
perf report
```

Call stacks are only reliable when the program and its libraries keep frame pointers, or when
you record with `--call-graph dwarf` (slower, with much larger output). Reading counters and
sampling the kernel may need `kernel.perf_event_paranoid` lowered, or root.

---

## Further reading

- LLVM: [Benchmarking tips](https://llvm.org/docs/Benchmarking.html)
- [perf wiki](https://perfwiki.github.io/main/), `perf-stat(1)`, `perf-record(1)`
- Arch wiki: [Benchmarking](https://wiki.archlinux.org/title/Benchmarking)
- Brendan Gregg: [perf Examples](https://www.brendangregg.com/perf.html)
- [hyperfine](https://github.com/sharkdp/hyperfine)

---

## In sysforge

`sysforge bench toolchain` applies these rules to the compiler it builds: it compiles a fixed
workload with the stock and the optimized clang, alternating between them after a warm-up,
and reports each one's median and spread. The "Did it help?" section of the
[PGO guide](../guides/pgo.md#did-it-help) covers it.
