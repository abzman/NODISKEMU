# Regression tests for the fixes on top of 1.2.0

Each test pulls the real function(s) out of the source tree, compiles them
on the host with small stubs (simulated pins, bus timing, disk image, FAT
file, I2C clock) and checks the behaviour that the matching commit fixes.

```
python3 testcode/fixes-1.2.0/before_after.py               # 1.2.0 vs. this tree
python3 testcode/fixes-1.2.0/before_after.py --per-commit  # plus every commit in between
python3 testcode/fixes-1.2.0/fixtests.py [--root PATH]     # just one tree
```

Requirements: git, python3 and gcc. On Windows, use WSL (simplest) or MSYS2
with the mingw-w64 gcc. No AVR toolchain or hardware is needed.

`before_after.py` checks out 1.2.0 (2b10add) into a temporary git worktree,
runs the same tests against it and against your working copy, and prints
both results side by side. It exits with 0 only if every test fails on
1.2.0 and passes on the fixed tree. `--per-commit` also runs the tests at
every commit in between, which shows each test flipping from fail to pass
at exactly the commit that fixes it.

| # | What is tested | How it fails on 1.2.0 |
|---|---|---|
| 1 | `time_after()` near the tick counter wrap | a new timeout expires immediately |
| 2 | `d64_unmount()` with two mounted images | BAM buffers freed while still in use, leaked after the last unmount |
| 3 | D64 directory chain linking to sector 0 | BAM read as directory, endless listing |
| 4 | D64 entry with file type 7 | listed as DEL instead of "???" |
| 5 | `I` right after `CD:...` | runs the CD handler instead of Initialize |
| 6 | `M-R` without length | no answer on channel 15 |
| 7 | `UC`..`UH` | jump to $0530.. instead of $0500.. |
| 8, 9 | empty swap list | endless loop; list left active |
| 10 | directory read without a valid card | 71 instead of 74,DRIVE NOT READY |
| 11 | `LOAD"*"` after SAVE into a D64 | points at the wrong directory entry |
| 12 | `LOAD"*"` after `OPEN 2,8,2,...` | loads the data file instead of the last program |
| 13 | FAT names with trailing space/dot or `< > \|` | passed to FatFs unencoded |
| 14 | 16+ buffers on petSD+/cbmSD-mini | dirty count and LED wrong, free-buffer check always true |
| 15 | IFC while ATN is held after DATA | endless loop |
| 16 | controller stops mid-command (no IFC) | endless loop |
| 17 | soft I2C timing at 16 MHz | 3 µs SCL phases, too fast for the PCF8583 RTC |
| 18 | ATtiny IO_IEC write with a register added after it | write leaks into the next register |

One extra line is marked INFO and is not a pass/fail test: while IFC is
held asserted, `ieee488_ProcessIFC()` still waits for it to be released, by
design. If a switched-off PET pulls IFC low, petSD+ keeps showing
"IFC: interface clear" until the PET is switched on.

These are host tests of the changed logic. They do not replace a test on
a real petSD+.
