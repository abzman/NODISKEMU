#!/usr/bin/env python3
"""Regression tests for the 18 fixes on top of NODISKEMU 1.2.0.

Every test extracts the real function(s) from the source tree given with
--root, compiles them on the host with small stubs and checks behaviour.
Point it at 1.2.0 and the tests for the fixed bugs fail; point it at the
fixed tree and they pass. before_after.py does both and prints a table.

Run:  python3 fixtests.py [--root PATH] [-v]
Needs: python3 and gcc (Linux, macOS, WSL or MSYS2). No AVR tools needed.

These are host tests: they check the logic of the changed code, with the
hardware (pins, SD card, I2C slaves, clock) replaced by simulations.
They do not replace a test on a real petSD+.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VERBOSE = False

# ---------------------------------------------------------------- helpers

class Skip(Exception):
    pass


def src(rel):
    return (ROOT / rel).read_text(encoding='latin-1')


def function(path, name):
    """Return the full text of C function `name` from file `path`."""
    s = src(path)
    m = re.search(r'^(?:static\s+|inline\s+)*[A-Za-z_][\w \t\*]*?\b' + re.escape(name)
                  + r'\s*\([^;{}]*\)\s*\{', s, re.M)
    if not m:
        raise Skip(f'{name}() not found in {path}')
    i = s.index('{', m.start()) + 1
    depth = 1
    while depth:
        c = s[i]
        if s.startswith('/*', i):
            i = s.index('*/', i) + 2
            continue
        if s.startswith('//', i):
            i = s.index('\n', i)
            continue
        if c in '"\'':
            j = i + 1
            while s[j] != c:
                j += 2 if s[j] == '\\' else 1
            i = j + 1
            continue
        depth += (c == '{') - (c == '}')
        i += 1
    return s[m.start():i]


def define(path, name, default=None):
    """Return the #define for `name` (with continuation lines) or a default."""
    s = src(path)
    m = re.search(r'^#\s*define\s+' + re.escape(name) + r'\b(?:[^\n]*\\\n)*[^\n]*', s, re.M)
    if m:
        return m.group(0) + '\n'
    if default is None:
        raise Skip(f'#define {name} not found in {path}')
    return f'#define {name} {default}\n'


def defines(path, *names):
    return ''.join(define(path, n) for n in names)


def has_define(path, name):
    return re.search(r'^#\s*define\s+' + re.escape(name) + r'\b', src(path), re.M) is not None


def run_c(code, extra_flags=(), files=None, expect_compile=True):
    """Compile and run a C program. Returns (rc, stdout+stderr)."""
    with tempfile.TemporaryDirectory(prefix='fixtests-') as tmp:
        tmp = Path(tmp)
        for rel, text in (files or {}).items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        (tmp / 't.c').write_text(code)
        exe = tmp / ('t.exe' if os.name == 'nt' else 't')
        cc = ['gcc', '-std=gnu99', '-O1', '-g', '-w', '-I' + str(tmp),
              *extra_flags, str(tmp / 't.c'), '-o', str(exe)]
        r = subprocess.run(cc, capture_output=True, text=True)
        if r.returncode:
            if not expect_compile:
                return None, r.stderr
            raise RuntimeError('compile failed:\n' + r.stderr[-3000:] + '\n--- code ---\n'
                               + '\n'.join(f'{n+1:4} {l}' for n, l in enumerate(code.splitlines())))
        try:
            p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return 124, 'HANG: test program did not finish within 20 s'


def expect(rc, out):
    """Turn a test program result into (passed, message)."""
    lines = [l for l in out.strip().splitlines() if l.strip()]
    msg = lines[-1] if lines else f'exit code {rc}'
    return rc == 0, msg


# Common C prelude: assertion that reports and exits instead of aborting,
# and a poll counter that turns endless loops into a reported "hang".
PRELUDE = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(cond, ...) do { if (!(cond)) { printf(__VA_ARGS__); printf("\n"); exit(1); } } while (0)
static unsigned long polls, poll_limit = 1000000;
static void poll(const char *what) {
  if (++polls > poll_limit) { printf("HANG: %s never ends (endless loop)\n", what); exit(2); }
}
'''

TESTS = []


def test(commit, title):
    def deco(fn):
        TESTS.append((commit, title, fn))
        return fn
    return deco

# ------------------------------------------------------------------ tests


@test(1, 'timer.h: time_after() at counter wrap-around')
def t_time_after():
    macro = define('src/timer.h', 'time_after')
    # On AVR, tick_t is 16 bit and int is 16 bit, so the subtraction in the
    # old macro can overflow a signed int: undefined behaviour that avr-gcc
    # folds into a plain signed comparison. A host int is 32 bit, so the
    # same macro is exercised with 32-bit ticks: identical C semantics,
    # just scaled up, and gcc folds it the same way.
    code = PRELUDE + r'''
typedef uint32_t tick_t;
typedef int32_t  stick_t;
''' + macro + r'''
__attribute__((noinline)) int after(tick_t a, tick_t b) { return time_after(a, b); }
int main(void) {
  /* a 25-tick timeout started just before the signed wrap point */
  tick_t now = 0x7ffffff0u, timeout = now + 25;
  CHECK(!after(now, timeout),
        "timeout started at tick 0x%lx expires immediately (signed wrap)", (unsigned long)now);
  CHECK(after(timeout + 1, timeout), "timeout never expires after the wrap");
  /* and at the unsigned wrap point */
  now = 0xfffffff0u; timeout = now + 25;
  CHECK(!after(now, timeout) && after(timeout + 1, timeout), "wrong result at unsigned wrap");
  printf("timeouts started next to the wrap point behave correctly\n");
  return 0;
}
'''
    return expect(*run_c(code, ['-O2']))


@test(2, 'd64_unmount(): BAM buffers with two mounted images')
def t_d64_unmount():
    code = PRELUDE + r'''
typedef struct buffer_s {
  uint8_t (*cleanup)(struct buffer_s *);
  uint8_t allocated;
  struct { struct { uint8_t part; } bam; } pvt;
} buffer_t;
static buffer_t b1, b2;
static buffer_t *bam_buffer, *bam_buffer2;
static uint8_t bam_refcount;
static uint8_t cleanup(buffer_t *b) { return 0; }
static void free_buffer(buffer_t *b) { if (b) b->allocated = 0; }
''' + function('src/d64ops.c', 'd64_unmount') + r'''
int main(void) {
  b1.cleanup = b2.cleanup = cleanup;
  b1.allocated = b2.allocated = 1;
  bam_buffer = &b1; bam_buffer2 = &b2;
  bam_refcount = 2;                     /* images mounted on partitions 0 and 1 */
  d64_unmount(0);
  CHECK(bam_buffer && b1.allocated && b2.allocated,
        "unmounting one image freed the BAM buffers still used by the other one");
  d64_unmount(1);
  CHECK(!bam_buffer && !bam_buffer2 && !b1.allocated && !b2.allocated,
        "BAM buffers stay allocated after the last image was unmounted (leak)");
  printf("BAM buffers are kept while in use and freed after the last unmount\n");
  return 0;
}
'''
    return expect(*run_c(code))


D64_DH = r'''
typedef struct { uint8_t track, sector, entry; } d64dh_t;
typedef struct { uint8_t part; struct { d64dh_t d64; } dir; } dh_t;
static uint8_t ops_scratch[256];
static uint8_t disk[36][21][256];             /* fake image: [track][sector] */
static uint8_t last_error, last_err_t, last_err_s;
#define LAST_TRACK 0
static uint8_t get_param(uint8_t part, uint8_t which) { return 35; }
static uint8_t sectors_per_track(uint8_t part, uint8_t t) { return t <= 17 ? 21 : t <= 24 ? 19 : t <= 30 ? 18 : 17; }
static void set_error_ts(uint8_t e, uint8_t t, uint8_t s) { last_error = e; last_err_t = t; last_err_s = s; }
static uint8_t checked_read(uint8_t part, uint8_t t, uint8_t s, uint8_t *buf, uint16_t len, uint8_t err) {
  poll("reading the directory chain");
  memcpy(buf, disk[t][s], len); return 0;
}
static uint8_t read_entry(uint8_t part, d64dh_t *dh, uint8_t *buf) {
  memcpy(buf, disk[dh->track][dh->sector] + 32 * dh->entry, 32); return 0;
}
#define ERROR_ILLEGAL_TS_LINK 66
'''


@test(3, 'D64: directory chain that links to sector 0')
def t_nextdirentry():
    code = PRELUDE + D64_DH + function('src/d64ops.c', 'nextdirentry') + r'''
int main(void) {
  /* 18/0 is the BAM, which links to the first directory sector 18/1.  */
  disk[18][0][0] = 18; disk[18][0][1] = 1;
  /* 18/1 holds 8 entries; its link is 18/0 (written by a buggy tool). */
  for (int e = 0; e < 8; e++) disk[18][1][32 * e + 2] = 0x82;
  disk[18][1][0] = 18; disk[18][1][1] = 0;
  dh_t dh = { 0, { { 18, 1, 0 } } };
  int entries = 0, res;
  poll_limit = 100;
  while ((res = nextdirentry(&dh)) == 0)
    entries++;
  CHECK(res == -1 && entries == 8,
        "directory listing read the BAM as directory (%d entries, result %d)", entries, res);
  printf("directory ends at the link to sector 0 (8 entries)\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(4, 'D64: directory entry with an invalid file type')
def t_invalid_type():
    consts = defines('src/dirent.h', 'TYPE_MASK', 'TYPE_DEL', 'TYPE_PRG', 'TYPE_DIR',
                     'FLAG_HIDDEN', 'FLAG_SPLAT', 'CBM_NAME_LENGTH')
    consts += define('src/dirent.h', 'EXT_TYPE_MASK', '15')
    consts += defines('src/d64ops.h', 'DIR_OFS_FILE_TYPE', 'DIR_OFS_FILE_NAME', 'DIR_OFS_YEAR',
                      'DIR_OFS_MONTH', 'DIR_OFS_DAY', 'DIR_OFS_HOUR', 'DIR_OFS_MINUTE',
                      'DIR_OFS_SIZE_LOW', 'DIR_OFS_SIZE_HI')
    code = PRELUDE + D64_DH + consts + r'''
#define OPSTYPE_DXX 2
#ifndef TYPE_INV
#define TYPE_INV 7
#endif
typedef struct {
  uint8_t opstype, typeflags, remainder;
  uint16_t blocksize;
  uint8_t name[CBM_NAME_LENGTH + 1];
  struct { uint8_t year, month, day, hour, minute; } date;
  union { struct { d64dh_t dh; } dxx; } pvt;
} cbmdirent_t;
static void strnsubst(uint8_t *s, uint8_t n, uint8_t from, uint8_t to) {
  for (; n; n--, s++) if (*s == from) *s = to;
}
''' + function('src/d64ops.c', 'nextdirentry') + function('src/d64ops.c', 'd64_readdir') + r'''
int main(void) {
  /* one directory sector with a single entry of file type 7 (closed). */
  /* A 1541 lists such an entry as "???"; it is not a deleted file.   */
  disk[18][1][2] = 0x87;
  memset(disk[18][1] + 5, 0xa0, 16); memcpy(disk[18][1] + 5, "ODDFILE", 7);
  dh_t dh = { 0, { { 18, 1, 0 } } };
  cbmdirent_t dent;
  poll_limit = 100;
  CHECK(d64_readdir(&dh, &dent) == 0, "entry not read");
  uint8_t type = dent.typeflags & EXT_TYPE_MASK;
  CHECK(type == TYPE_INV, "invalid file type 7 is listed as type %d (%s) instead of \"???\"",
        type, type == TYPE_DEL ? "DEL" : "?");
  printf("invalid file type is kept as TYPE_INV and listed as \"???\"\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(5, 'single-character DOS command after a CD command')
def t_one_char_cmd():
    code = PRELUDE + r'''
#define CONFIG_COMMAND_BUFFER_SIZE 120
#define ERROR_OK 0
#define ERROR_SYNTAX_TOOLONG 32
#define ERROR_SYNTAX_UNABLE 31
#define ERROR_SYNTAX_UNKNOWN 30
static uint8_t command_buffer[CONFIG_COMMAND_BUFFER_SIZE + 2];
static uint8_t command_length, original_length;
static const char *called;
static void set_error(uint8_t e) {}
static void display_doscommand(uint8_t l, uint8_t *b) {}
#define H(n) static void n(void) { called = #n; }
H(parse_dircommand) H(parse_block) H(parse_changepart) H(parse_copy) H(parse_direct)
H(parse_getpartition) H(parse_initialize) H(parse_memory) H(parse_new) H(parse_position)
H(parse_rename) H(parse_scratch) H(parse_time) H(parse_user) H(parse_xcommand)
''' + function('src/doscmd.c', 'parse_doscommand') + r'''
static void send(const char *cmd) {
  command_length = strlen(cmd);
  memcpy(command_buffer, cmd, command_length);   /* old bytes stay behind it */
  called = "nothing";
  parse_doscommand();
}
int main(void) {
  send("CD:GAMES");
  CHECK(!strcmp(called, "parse_dircommand"), "CD not recognized (%s)", called);
  send("I");               /* Initialize, command_buffer[1] still holds 'D' */
  CHECK(!strcmp(called, "parse_initialize"),
        "\"I\" after \"CD:...\" was run as %s() instead of Initialize", called);
  printf("one-character commands ignore leftovers from the previous command\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(6, 'M-R without a length byte')
def t_memread():
    code = PRELUDE + r'''
typedef unsigned UINT;
typedef int FRESULT;
#define FR_OK 0
#define FA_READ 1
#define FA_OPEN_EXISTING 0
#define ERRORBUFFER_IDX 0
#define PROGMEM
#define pgm_read_word(p) (*(p))
#define pgm_read_byte(p) (*(p))
typedef struct { uint32_t fsize; } FIL;
typedef struct { uint16_t address; uint8_t val[2]; } magic_value_t;
static const magic_value_t c1541_magics[] = { { 0xe5c6, { 0x34, 0xb1 } }, { 0, { 0, 0 } } };
static struct { struct { uint32_t curr_dir; } fatfs; struct { uint32_t fat; } current_dir; } partition[1];
static uint8_t current_part, rom_filename[1];
static FIL romfile;
static FRESULT f_open(void *fs, FIL *f, uint8_t *n, uint8_t m) { return 1; }
static FRESULT f_lseek(FIL *f, uint32_t o) { return 1; }
static FRESULT f_read(FIL *f, void *b, UINT n, UINT *r) { return 1; }
static uint8_t command_buffer[64], command_length, error_buffer[48];
static struct { uint8_t *data; uint8_t position, lastused; } buffers[1];
''' + function('src/doscmd.c', 'handle_memread') + r'''
int main(void) {
  /* "M-R" + address $E5C6 and no length: a drive returns one byte.   */
  /* (programs use this to detect the drive type, e.g. 1541 vs 1571) */
  memcpy(command_buffer, "M-R\xc6\xe5", 5); command_length = 5;
  buffers[0].lastused = 99;
  handle_memread();
  CHECK(buffers[0].data == error_buffer && buffers[0].lastused == 0 && error_buffer[0] == 0x34,
        "M-R without length returned nothing (channel 15 not set up)");
  printf("M-R without length returns one byte\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(7, 'UC..UH user commands run the right buffer address')
def t_user_cmds():
    code = PRELUDE + r'''
#define ERROR_SYNTAX_UNKNOWN 30
#define ERROR_DOSVERSION 73
#define VC20MODE 1
#define FMB_USER 0
static uint8_t command_buffer[64], command_length, device_address, globalflags;
static uint16_t loader_address;
static void system_reset(void) {}
static void display_address(uint8_t a) {}
static void lcd_update_device_addr(void) {}
static void set_error(uint8_t e) {}
static void parse_block(void) {}
static void free_multiple_buffers(uint8_t f) {}
static void run_loader(uint16_t a) { loader_address = a; }
''' + function('src/doscmd.c', 'parse_user') + r'''
static uint16_t user(const char *c) {
  loader_address = 0; strcpy((char *)command_buffer, c); command_length = strlen(c);
  parse_user(); return loader_address;
}
int main(void) {
  /* U3..U8 and their aliases UC..UH jump to $0500 + 3*n */
  const char *a = "345678", *b = "CDEFGH";
  for (int i = 0; i < 6; i++) {
    char n[3] = { 'U', a[i], 0 }, m[3] = { 'U', b[i], 0 };
    uint16_t want = 0x500 + 3 * i;
    CHECK(user(n) == want, "%s ran $%04X, expected $%04X", n, user(n), want);
    CHECK(user(m) == want, "%s ran $%04X instead of $%04X like %s", m, user(m), want, n);
  }
  printf("U3..U8 and UC..UH all jump to $0500..$050F\n");
  return 0;
}
'''
    return expect(*run_c(code))


def swaplist_code(final_check):
    marker = re.search(r'static const[^;]*petscii_marker\[\d*\][^;]*;', src('src/diskchange.c'), re.S)
    if not marker:
        raise Skip('petscii_marker not found')
    marker = marker.group(0).replace('PROGMEM', '').replace('NONSTRING', '')
    code = PRELUDE + r'''
#define CONFIG_COMMAND_BUFFER_SIZE 120
#define MAX_LINE_LEN (CONFIG_COMMAND_BUFFER_SIZE - 1)
#define FR_OK 0
#define FA_READ 1
#define FA_OPEN_EXISTING 0
#define ERROR_OK 0
#define ERROR_DOSVERSION 73
#define ERROR_FILE_NOT_FOUND 62
#define FMB_USER_CLEAN 0
#define BLINK_HOME 0
#define SWAPLIST_ASCII 2
#define AUTOSWAP_ACTIVE 4
#define memcmp_P memcmp
typedef unsigned UINT;
typedef int FRESULT;
typedef struct { void *fs; uint32_t fsize, fptr; } FIL;
typedef struct { uint32_t fat; } dir_t;
typedef struct { uint8_t part; dir_t dir; } path_t;
typedef struct { uint8_t data[256]; } buffer_t;
static int fatops, fs_object;
static struct { struct { uint32_t curr_dir; } fatfs; void *fop; dir_t current_dir; } partition[2];
static FIL swaplist;
static path_t swappath;
static uint8_t linenum, globalflags, current_error, current_part, command_buffer[CONFIG_COMMAND_BUFFER_SIZE + 2];
static const char *file_content;
static char mounted[64];
static buffer_t workbuf;
''' + marker + r'''
static FRESULT f_open(void *fs, FIL *f, uint8_t *n, uint8_t m) {
  f->fs = &fs_object; f->fsize = strlen(file_content); f->fptr = 0; return FR_OK;
}
static FRESULT f_close(FIL *f) { f->fs = NULL; return FR_OK; }
static FRESULT f_lseek(FIL *f, uint32_t o) { f->fptr = o; return FR_OK; }
static FRESULT f_read(FIL *f, void *b, UINT n, UINT *r) {
  poll("reading the swap list");
  UINT left = f->fsize > f->fptr ? f->fsize - f->fptr : 0;
  *r = n < left ? n : left; memcpy(b, file_content + f->fptr, *r); f->fptr += *r; return FR_OK;
}
static void parse_error(FRESULT r, uint8_t f) { current_error = 74; }
static void set_error(uint8_t e) { current_error = e; }
static void free_multiple_buffers(uint8_t f) {}
static buffer_t *alloc_buffer(void) { return &workbuf; }
static void image_unmount(uint8_t p) {}
static void display_current_part(uint8_t p) {}
static void asc2pet(uint8_t *s) {}
static void do_chdir(uint8_t *s) { strcpy(mounted, (char *)s); }
static void confirm_blink(uint8_t t) {}
static uint8_t ustrlen(uint8_t *s) { return strlen((char *)s); }
''' + function('src/diskchange.c', 'mount_line') + function('src/diskchange.c', 'set_changelist_internal') + r'''
int main(void) {
  path_t p = { 0 };
  partition[0].fop = &fatops;
  poll_limit = 10000;
  /* control: a normal swap list mounts its first entry */
  file_content = "game1.d64\r\ngame2.d64\r\n";
  set_changelist_internal(&p, (uint8_t *)"SWAP.LST", 0);
  CHECK(!strcmp(mounted, ":game1.d64"), "first entry not mounted (%s)", mounted);
  /* an empty swap list (e.g. created by mistake) */
  mounted[0] = 0; polls = 0;
  file_content = "";
  set_changelist_internal(&p, (uint8_t *)"EMPTY.LST", 0);
''' + final_check + r'''
  return 0;
}
'''
    return code


@test(8, 'empty swap list file: no endless loop')
def t_swaplist_hang():
    return expect(*run_c(swaplist_code(r'''
  CHECK(mounted[0] == 0, "something was mounted from an empty swap list");
  printf("an empty swap list no longer hangs the drive\n");''')))


@test(9, 'empty swap list file: closed instead of left active')
def t_swaplist_closed():
    return expect(*run_c(swaplist_code(r'''
  CHECK(swaplist.fs == NULL, "the empty swap list stays open and active (buttons keep trying it)");
  printf("an empty swap list is closed right away, a normal one still mounts\n");''')))


@test(10, 'fat_readdir(): error for an invalid FAT object')
def t_fat_readdir():
    ff = src('src/ff.h')
    enum = re.search(r'typedef enum \{[^}]*FR_OK[^}]*\} FRESULT;', ff, re.S).group(0)
    errs = ''.join(define('src/errormsg.h', n) for n in (
        'ERROR_OK', 'ERROR_FILE_NOT_FOUND', 'ERROR_FILE_NOT_FOUND_39', 'ERROR_SYNTAX_JOKER',
        'ERROR_DRIVE_NOT_READY', 'ERROR_READ_NOHEADER', 'ERROR_WRITE_VERIFY', 'ERROR_WRITE_PROTECT',
        'ERROR_FILE_EXISTS', 'ERROR_DISK_FULL', 'ERROR_SYNTAX_UNABLE', 'ERROR_DIR_ERROR'))
    body = function('src/fatops.c', 'fat_readdir')
    loop = re.search(r'  do \{\n    res = f_readdir.*?\} while \(.*?\);\n', body, re.S)
    if not loop:
        raise Skip('fat_readdir() read loop not found')
    code = PRELUDE + enum + '\n' + errs + r'''
#define AM_VOL 8
static uint8_t current_error;
static void set_error(uint8_t e) { current_error = e; }
static void set_error_ts(uint8_t e, uint8_t t, uint8_t s) { current_error = e; }
typedef struct { uint8_t fname[13], fattrib; uint8_t *lfn; } FILINFO;
static FRESULT f_readdir(void *d, FILINFO *f) { return FR_INVALID_OBJECT; }
''' + function('src/fatops.c', 'parse_error') + r'''
static int8_t readdir_errorpath(void) {
  FRESULT res; FILINFO finfo;
  struct { struct { void *fat; } dir; } dhs, *dh = &dhs;
''' + loop.group(0) + r'''
  return 0;
}
int main(void) {
  /* FatFs reports FR_INVALID_OBJECT when there is no card or no valid */
  /* file system: a 1541 would say "74,DRIVE NOT READY"                */
  CHECK(readdir_errorpath() == 1, "error not reported");
  CHECK(current_error == ERROR_DRIVE_NOT_READY,
        "directory read without a valid card gives error %d instead of 74,DRIVE NOT READY",
        current_error);
  printf("missing card/file system is reported as 74,DRIVE NOT READY\n");
  return 0;
}
'''
    return expect(*run_c(code))


FILE_OPEN_ENV = r'''
#define CBM_NAME_LENGTH 16
#define CONFIG_COMMAND_BUFFER_SIZE 120
#define TYPE_MASK 7
#define TYPE_DEL 0
#define TYPE_SEQ 1
#define TYPE_PRG 2
#define TYPE_USR 3
#define TYPE_REL 4
#define TYPE_DIR 6
#define TYPE_INV 7
#define FLAG_HIDDEN (1<<5)
#define ERROR_OK 0
#define ERROR_SYNTAX_UNABLE 31
#define ERROR_FILE_TYPE_MISMATCH 64
#define ERROR_NO_CHANNEL 70
#define ERROR_FILE_EXISTS 63
#define ERROR_FILE_NOT_FOUND 62
#define uart_trace(a,b,c)
enum open_modes { OPEN_READ, OPEN_WRITE, OPEN_APPEND, OPEN_MODIFY };
typedef struct { uint8_t track, sector, entry; } d64dh_t;
typedef struct { uint8_t typeflags; uint8_t name[CBM_NAME_LENGTH + 1];
                 union { struct { d64dh_t dh; } dxx; } pvt; } cbmdirent_t;
typedef struct { uint8_t part; } path_t;
typedef struct { uint8_t secondary, read, write, allocated; } buffer_t;
typedef struct { int dummy; } dh_t;
static dh_t matchdh;
static uint8_t command_buffer[CONFIG_COMMAND_BUFFER_SIZE + 2], command_length, current_error;
static path_t previous_file_path;
static cbmdirent_t previous_file_dirent;
static buffer_t thebuf;
static const char *disk_file;                /* name of the one file on the disk */
static const d64dh_t disk_file_pos = { 18, 1, 2 }, new_file_pos = { 18, 4, 5 };
static uint8_t matched;
static void set_error(uint8_t e) { current_error = e; }
static buffer_t *find_buffer(uint8_t sa) { return NULL; }
static void cleanup_and_free_buffer(buffer_t *b) {}
static void open_buffer(uint8_t sa) {}
static void file_open_previous(void) {}
static uint8_t *ustrchr(uint8_t *s, uint8_t c) { return (uint8_t *)strchr((char *)s, c); }
static void ustrncpy(uint8_t *d, uint8_t *s, uint8_t n) { strncpy((char *)d, (char *)s, n); }
static void load_directory(uint8_t sa) {}
static uint8_t parse_path(uint8_t *in, path_t *p, uint8_t **name, uint8_t x) {
  p->part = 0; *name = in; return 0;
}
static uint8_t opendir(dh_t *d, path_t *p) { matched = 0; return 0; }
static int8_t next_match(dh_t *d, uint8_t *name, void *a, void *b, uint8_t f, cbmdirent_t *dent) {
  memset(dent, 0, sizeof(*dent));
  if (matched++ || strcmp((char *)name, disk_file)) return -1;     /* not found */
  strcpy((char *)dent->name, disk_file); dent->typeflags = TYPE_PRG;
  dent->pvt.dxx.dh = disk_file_pos;
  return 0;
}
static uint8_t check_free_buffers(void) { return 1; }
static uint8_t file_delete(path_t *p, cbmdirent_t *d) { return 0; }
static buffer_t *alloc_buffer(void) { memset(&thebuf, 0, sizeof thebuf); thebuf.allocated = 1; return &thebuf; }
static void free_buffer(buffer_t *b) { b->allocated = 0; }
static void display_filename_read(uint8_t p, uint8_t l, uint8_t *n) {}
static void display_filename_write(uint8_t p, uint8_t l, uint8_t *n) {}
static void open_rel(path_t *p, cbmdirent_t *d, buffer_t *b, uint8_t l, uint8_t m) {}
static void open_read(path_t *p, cbmdirent_t *d, buffer_t *b) { b->read = 1; }
/* Opening a new file in a D64 creates its directory entry; the fixed */
/* d64_open_write() records that position in dent (see test 11b).    */
static void open_write(path_t *p, cbmdirent_t *d, uint8_t t, buffer_t *b, uint8_t a) {
  d->pvt.dxx.dh = new_file_pos; b->write = 1;
}
void file_open(uint8_t secondary);
static void send(uint8_t sa, const char *name) {
  command_length = strlen(name); strcpy((char *)command_buffer, name);
  file_open(sa);
}
'''


def file_open_code():
    code = function('src/fileops.c', 'file_open')
    free = ''
    if 'free_unopened_buffer' in code:
        free = function('src/fileops.c', 'free_unopened_buffer')
    return free + code


@test(11, 'LOAD"*" after SAVE remembers where the new file is')
def t_load_star_save():
    code = PRELUDE + FILE_OPEN_ENV + file_open_code() + r'''
int main(void) {
  disk_file = "OTHER";
  send(1, "NEWPROG");                          /* SAVE "NEWPROG",8 */
  CHECK(!strcmp((char *)previous_file_dirent.name, "NEWPROG"), "SAVE not remembered");
  d64dh_t p = previous_file_dirent.pvt.dxx.dh;
  CHECK(p.track == new_file_pos.track && p.sector == new_file_pos.sector && p.entry == new_file_pos.entry,
        "LOAD\"*\" after SAVE would use directory slot %d/%d/%d, not the new file at %d/%d/%d",
        p.track, p.sector, p.entry, new_file_pos.track, new_file_pos.sector, new_file_pos.entry);
  printf("after SAVE, LOAD\"*\" points at the saved file's directory entry\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(11, 'd64_open_write() records the new directory entry')
def t_d64_open_write():
    code = PRELUDE + r'''
#define CBM_NAME_LENGTH 16
#define FA_WRITE 2
#define ERROR_WRITE_PROTECT 26
#define DIR_OFS_FILE_TYPE 2
#define DIR_OFS_TRACK 3
#define DIR_OFS_SECTOR 4
#define DIR_OFS_FILE_NAME 5
#define DIR_OFS_SIZE_LOW 0x1e
#define DIR_OFS_SIZE_HI 0x1f
typedef struct { uint8_t track, sector, entry; } d64dh_t;
typedef struct { uint8_t part; struct { d64dh_t d64; } dir; } dh_t;
typedef struct { uint8_t part; } path_t;
typedef struct { uint8_t typeflags; uint8_t name[CBM_NAME_LENGTH + 1];
                 union { struct { d64dh_t dh; } dxx; } pvt; } cbmdirent_t;
typedef struct buffer_s {
  uint8_t data[256], read, position, lastused, mustflush;
  uint8_t (*refill)(struct buffer_s *), (*cleanup)(struct buffer_s *), (*seek)(struct buffer_s *, uint32_t, uint8_t);
  struct { struct { d64dh_t dh; uint16_t blocks; uint8_t part, track, sector; } d64; } pvt;
} buffer_t;
static struct { struct { uint8_t flag; } imagehandle; } partition[1];
static uint8_t ops_scratch[256], current_error;
static void set_error(uint8_t e) { current_error = e; }
static void d64_open_read(path_t *p, cbmdirent_t *d, buffer_t *b) {}
static uint8_t d64_write(buffer_t *b) { return 0; }
static uint8_t d64_write_cleanup(buffer_t *b) { return 0; }
static uint8_t d64_seek(buffer_t *b, uint32_t p, uint8_t i) { return 0; }
static void mark_write_buffer(buffer_t *b) {}
static void update_timestamp(uint8_t *e) {}
static uint8_t write_entry(uint8_t p, d64dh_t *dh, uint8_t *e, uint8_t f) { return 0; }
static uint8_t get_first_sector(uint8_t p, uint8_t *t, uint8_t *s) { *t = 17; *s = 0; return 0; }
static uint8_t allocate_sector(uint8_t p, uint8_t t, uint8_t s) { return 0; }
static uint8_t find_empty_entry(path_t *p, dh_t *dh) {   /* free slot: 18/4 entry 5 */
  dh->dir.d64.track = 18; dh->dir.d64.sector = 4; dh->dir.d64.entry = 5; return 0;
}
''' + function('src/d64ops.c', 'd64_open_write') + r'''
int main(void) {
  path_t p = { 0 }; cbmdirent_t dent = { 0 }; buffer_t buf = { 0 };
  partition[0].imagehandle.flag = FA_WRITE;
  strcpy((char *)dent.name, "NEWPROG");
  d64_open_write(&p, &dent, 0x82, &buf, 0);
  d64dh_t d = dent.pvt.dxx.dh;
  CHECK(d.track == 18 && d.sector == 4 && d.entry == 5,
        "the new file's directory position (18/4/5) is not recorded, LOAD\"*\" can't find it (%d/%d/%d)",
        d.track, d.sector, d.entry);
  printf("SAVE into a D64 records the new directory entry for LOAD\"*\"\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(12, 'LOAD"*" is not changed by OPEN on a data channel')
def t_load_star_sa():
    code = PRELUDE + FILE_OPEN_ENV + file_open_code() + r'''
int main(void) {
  disk_file = "GAME";
  send(0, "GAME");                             /* LOAD "GAME",8 */
  CHECK(!strcmp((char *)previous_file_dirent.name, "GAME"), "LOAD not remembered");
  disk_file = "SCORES";
  send(2, "SCORES,S,R");                       /* OPEN 2,8,2,"SCORES,S,R" */
  CHECK(!strcmp((char *)previous_file_dirent.name, "GAME"),
        "after OPEN 2,8,2,\"SCORES\" LOAD\"*\" would load %s instead of GAME",
        previous_file_dirent.name);
  printf("only LOAD/SAVE (SA 0/1) change the file used by LOAD\"*\"\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(13, 'FAT file name validation')
def t_fat_names():
    code = PRELUDE + function('src/fatops.c', 'is_valid_fat_char') + \
        function('src/fatops.c', 'is_valid_fat_name') + r'''
static bool valid(const char *n) { return is_valid_fat_name((const uint8_t *)n); }
int main(void) {
  /* names that FAT/FatFs can store as they are */
  CHECK(valid("HELLO") && valid("FILE.PRG") && valid("MY FILE"), "normal name rejected");
  /* FAT can't store these; they must be rejected so the P00 encoding is used */
  CHECK(!valid("HELLO "), "name with a trailing space passed as a FAT name");
  CHECK(!valid("HELLO."), "name with a trailing dot passed as a FAT name");
  CHECK(!valid("A<B") && !valid("A>B") && !valid("A|B"),
        "name with < > or | passed although FatFs rejects it");
  CHECK(!valid(""), "empty name passed");
  printf("names FAT can't store are rejected (they get P00-encoded)\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(14, 'buffer counters with 32 buffers (petSD+ / cbmSD-mini)')
def t_buffers():
    hdr = src('src/buffers.h')
    macros = ''.join(re.findall(r'^#define (?:check_free_buffers|get_dirty_buffer_count)\(\)[^\n]*\n', hdr, re.M))
    fns = ''.join(function('src/buffers.c', n) for n in (
        'callback_dummy', 'alloc_specific_buffer', 'alloc_system_buffer', 'alloc_buffer',
        'free_buffer', 'mark_buffer_dirty', 'mark_buffer_clean'))
    code = PRELUDE + r'''
#define CONFIG_BUFFER_COUNT 32
#define BUFFER_SEC_SYSTEM 100
#define ERROR_NO_CHANNEL 70
typedef struct buffer_s {
  uint8_t *data, secondary, allocated, dirty, read, write, position, lastused, mustflush, sendeoi, recordlen;
  uint8_t (*refill)(struct buffer_s *), (*cleanup)(struct buffer_s *);
  uint8_t (*seek)(struct buffer_s *, uint32_t, uint8_t);
} buffer_t;
static buffer_t buffers[CONFIG_BUFFER_COUNT + 1];
static uint8_t bufferdata[CONFIG_BUFFER_COUNT * 256];
uint8_t active_buffers, dirty_buffers;
''' + macros + r'''
static uint8_t busy_led, dirty_led;
static void set_busy_led(bool b) { busy_led = b; }
static void set_dirty_led(bool b) { dirty_led = b; }
static void update_leds(void) { busy_led = active_buffers != 0; dirty_led = get_dirty_buffer_count() != 0; }
static void set_error(uint8_t e) {}
''' + fns + r'''
int main(void) {
  buffer_t *b[CONFIG_BUFFER_COUNT];
  for (int i = 0; i < CONFIG_BUFFER_COUNT; i++) buffers[i].data = bufferdata + 256 * i;
  /* open 16 channels for reading (16 counted buffers, none dirty) */
  for (int i = 0; i < 16; i++) b[i] = alloc_buffer();
  CHECK(get_dirty_buffer_count() == 0 && !dirty_led,
        "16 read buffers open: dirty count is %d and the dirty LED is on", get_dirty_buffer_count());
  CHECK(check_free_buffers(), "free buffers reported missing");
  for (int i = 16; i < CONFIG_BUFFER_COUNT; i++) b[i] = alloc_buffer();
  CHECK(!check_free_buffers(), "all 32 buffers in use, but check_free_buffers() says there are free ones");
  for (int i = 0; i < CONFIG_BUFFER_COUNT; i++) free_buffer(b[i]);
  CHECK(active_buffers == 0 && get_dirty_buffer_count() == 0, "counters not back to 0");
  /* one dirty buffer */
  b[0] = alloc_buffer(); mark_buffer_dirty(b[0]);
  CHECK(get_dirty_buffer_count() == 1, "dirty buffer not counted");
  mark_buffer_clean(b[0]); free_buffer(b[0]);
  CHECK(active_buffers == 0 && get_dirty_buffer_count() == 0, "counters wrong after clean+free");
  printf("active and dirty counts stay correct with up to 32 buffers\n");
  return 0;
}
'''
    return expect(*run_c(code))


IEEE_ENV = r'''
/* ---- simulated IEEE-488 bus with simulated time ---- */
typedef uint16_t tick_t;
typedef int16_t stick_t;
#define HZ 100
#define MS_TO_TICKS(x) ((tick_t)((x) / (1000 / HZ)))
static unsigned long now_us;
static tick_t getticks(void) { return now_us / 10000; }
#define IEEE_UNLISTEN 0x3f
#define IEEE_UNTALK 0x5f
#define IEEE_LISTEN 0x20
#define IEEE_TALK 0x40
#define IEEE_SECONDARY 0x60
#define IEEE_CLOSE 0xe0
#define IEEE_OPEN 0xf0
#define TE_LISTEN 0
#define LL_RECEIVE 0
#define LL_OPEN 1
#define FMB_USER_CLEAN 0
#define PSTR(x) x
#define uart_puthex(x)
#define uart_putc(x)
#define uart_puts_P(x)
typedef struct buffer_s { uint8_t (*cleanup)(struct buffer_s *); } buffer_t;
static uint8_t device_address = 8, ieee488_TE75160 = TE_LISTEN, open_sa;
static volatile bool ieee488_ATN_received, ieee488_IFCreceived;
static uint8_t ieee488_ListenActive, ieee488_TalkingDevice;
static bool open_active, listen_loop_called, bus_idle_called;
/* Script of command bytes sent with ATN low. The controller puts a byte
   on the bus (DAV low), waits until the drive accepts it (NDAC high),
   then releases DAV. Each pin read takes 1 us of simulated time.       */
static const uint8_t *cmd_bytes; static int ncmd, pos;
static bool accepted, released;
static unsigned long t_released;
static bool atn_low_forever, ifc_after_cmds, dav_stuck_low;
static bool last_byte_done(void) { return pos >= ncmd || (pos == ncmd - 1 && released); }
static bool ieee488_ATN(void) {            /* true = ATN high (inactive) */
  poll("waiting for the bus"); now_us++;
  if (!last_byte_done()) return false;
  return !atn_low_forever;
}
static bool ieee488_DAV(void) {            /* true = DAV high */
  poll("waiting for the bus"); now_us++;
  if (pos >= ncmd) return true;            /* nothing more to send */
  if (!accepted) return false;             /* data valid */
  if (dav_stuck_low && pos == ncmd - 1) return false;
  if (!released) { released = true; t_released = now_us; }
  return true;
}
static uint8_t ieee488_Data(void) { return cmd_bytes[pos]; }
static void ieee488_SetNDAC(uint8_t x) {
  if (x) accepted = true;
  else if (accepted && released) { accepted = released = false; pos++; }
}
static void ieee488_SetNRFD(uint8_t x) {}
static void ieee488_DataListen(void) {}
static void ieee488_BusIdle(void) { bus_idle_called = true; }
static bool ieee488_CheckIFC(void) {          /* PET reset shortly after the last byte */
  if (ifc_after_cmds && pos == ncmd - 1 && released && now_us - t_released > 50) {
    ieee488_IFCreceived = true; return true;
  }
  return false;
}
static void ieee488_ProcessIFC(void) { ieee488_IFCreceived = false; }
static void ieee488_Unlisten(void) { ieee488_ListenActive = 0; }
static void ieee488_Untalk(void) { ieee488_TalkingDevice = 0; }
static void ieee488_ListenLoop(uint8_t a, uint8_t sa) { listen_loop_called = true; }
static void ieee488_TalkLoop(uint8_t sa) {}
static void free_multiple_buffers(uint8_t f) {}
static buffer_t *find_buffer(uint8_t sa) { return NULL; }
static void free_buffer(buffer_t *b) {}
static void scenario(const uint8_t *c, int n) {
  cmd_bytes = c; ncmd = n; pos = 0; accepted = released = false; polls = 0; now_us = 0;
  ieee488_ATN_received = true; ieee488_ListenActive = ieee488_TalkingDevice = 0;
  listen_loop_called = bus_idle_called = false;
  atn_low_forever = ifc_after_cmds = dav_stuck_low = false;
}
'''


def ieee_code():
    s = src('src/ieee.c')
    timeout = re.search(r'^#define IEEE_CMD_TIMEOUT_TICKS[^\n]*\n', s, re.M)
    return (define('src/timer.h', 'time_after') + (timeout.group(0) if timeout else '')
            + '#define printf(...)\n' + function('src/ieee.c', 'handle_ieee488') + '\n#undef printf\n')


@test(15, 'IEEE: IFC while waiting for ATN after a DATA command')
def t_ieee_ifc():
    code = PRELUDE + IEEE_ENV + ieee_code() + r'''
int main(void) {
  static const uint8_t listen_data[] = { IEEE_LISTEN | 8, IEEE_SECONDARY | 2 };
  /* control: normal command sequence, ATN released afterwards */
  scenario(listen_data, 2);
  handle_ieee488();
  CHECK(listen_loop_called, "normal LISTEN 8 / DATA 2 did not reach the listen loop");
  /* the PET is reset (IFC) while it still holds ATN low after DATA */
  scenario(listen_data, 2);
  atn_low_forever = true; ifc_after_cmds = true;
  poll_limit = 2000000;
  handle_ieee488();
  CHECK(ieee488_IFCreceived, "IFC not noticed");
  printf("a bus reset (IFC) after DATA is handled instead of hanging\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(16, 'IEEE: controller stops in the middle of a command')
def t_ieee_timeout():
    code = PRELUDE + IEEE_ENV + ieee_code() + r'''
int main(void) {
  static const uint8_t listen_only[] = { IEEE_LISTEN | 8 };
  static const uint8_t listen_data[] = { IEEE_LISTEN | 8, IEEE_SECONDARY | 2 };
  poll_limit = 5000000;                         /* = 5 s of simulated bus time */
  /* a) ATN stays low, no further command byte ever comes (no IFC) */
  scenario(listen_only, 1); atn_low_forever = true;
  handle_ieee488();
  CHECK(now_us < 1000000, "returned only after %lu ms", now_us / 1000);
  /* b) after DATA the controller never releases ATN (no IFC) */
  scenario(listen_data, 2); atn_low_forever = true;
  handle_ieee488();
  CHECK(now_us < 1000000, "returned only after %lu ms", now_us / 1000);
  /* c) DAV stays low after the drive accepted a byte */
  scenario(listen_only, 1); atn_low_forever = true; dav_stuck_low = true;
  handle_ieee488();
  CHECK(now_us < 1000000, "returned only after %lu ms", now_us / 1000);
  printf("a controller that stops mid-command is given up after ~250 ms\n");
  return 0;
}
'''
    return expect(*run_c(code))


@test(16, 'IEEE: PET holding IFC low (known limitation, not fixed)')
def t_ieee_ifc_held():
    # Documents behaviour, not a fixed bug: while IFC is asserted the drive
    # stays in ieee488_ProcessIFC() by design. If a switched-off PET pulls
    # IFC low, petSD+ shows "IFC: interface clear" until the PET is on again.
    body = function('src/ieee.c', 'ieee488_ProcessIFC')
    waits = re.search(r'while\s*\(\s*!\s*ieee488_IFC\s*\(\s*\)\s*\)\s*;', body)
    return ('INFO', 'not fixed: ProcessIFC still waits for IFC release (by design)') if waits else \
           ('INFO', 'ProcessIFC no longer blocks while IFC is held low')


@test(17, 'software I2C bit timing in IEEE-488 mode (16 MHz)')
def t_i2c_timing():
    code = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
static uint8_t ddr, port;
static int prescale;                       /* clock_div_1 = 16 MHz, clock_div_2 = 8 MHz */
static double t_us, run_us, min_low = 1e9, min_high = 1e9;
static int level = -1, runs;
static uint8_t pin(void) { return (~ddr) & 3; }          /* lines rise instantly */
static void delay(double us) {
  /* F_CPU is 8 MHz: at 16 MHz every _delay_us() takes half the time */
  double real = prescale == 0 ? us / 2 : us;
  int scl = !(ddr & 2);
  if (scl != level) {
    if (level >= 0 && runs++ > 1) {             /* skip the idle phase before START */
      if (level) { if (run_us < min_high) min_high = run_us; }
      else       { if (run_us < min_low)  min_low  = run_us; }
    }
    level = scl; run_us = 0;
  }
  run_us += real; t_us += real;
}
#include "softi2c.c"
int main(int argc, char **argv) {
  prescale = atoi(argv[1]);
  i2c_init();
  min_low = min_high = 1e9; runs = 0; level = -1;
  i2c_write_register(0xa0, 1, 0x55);
  printf("%.1f %.1f\n", min_low, min_high);
  return 0;
}
'''
    files = {
        'avr/io.h': '#define _BV(n) (1U << (n))\n',
        'avr/power.h': '#define clock_div_1 0\n#define clock_div_2 1\n#define clock_prescale_get() prescale\n',
        'util/delay.h': '#define _delay_us(us) delay(us)\n',
        'config.h': '#define HAVE_I2C\n#define IEC_SLOW_IEEE_FAST\n#define SOFTI2C_BIT_SDA 0\n'
                    '#define SOFTI2C_BIT_SCL 1\n#define SOFTI2C_DDR ddr\n#define SOFTI2C_PORT port\n'
                    '#define SOFTI2C_PIN pin()\n' + define('src/avr/arch-config.h', 'SOFTI2C_DELAY', '6'),
    }
    with tempfile.TemporaryDirectory(prefix='fixtests-i2c-') as tmp:
        tmp = Path(tmp)
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
        (tmp / 't.c').write_text(code)
        r = subprocess.run(['gcc', '-std=gnu99', '-O1', '-w', '-I' + str(tmp), '-I' + str(ROOT / 'src'),
                            '-I' + str(ROOT / 'src/avr'), str(tmp / 't.c'), '-o', str(tmp / 't.exe')],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr[-2000:])
        res = {}
        for name, ps in (('IEC (8 MHz)', 1), ('IEEE-488 (16 MHz)', 0)):
            out = subprocess.run([str(tmp / 't.exe'), str(ps)], capture_output=True, text=True, timeout=20).stdout
            res[name] = tuple(float(x) for x in out.split())
    # PCF8583 (petSD+ rev. 1.x RTC) is a 100 kHz part: tLOW >= 4.7 us, tHIGH >= 4.0 us
    bad = [f'{n}: SCL low {lo} us / high {hi} us' for n, (lo, hi) in res.items() if lo < 4.7 or hi < 4.0]
    if bad:
        return False, 'too fast for the 100 kHz PCF8583 RTC (needs >= 4.7/4.0 us): ' + '; '.join(bad)
    lo, hi = res['IEEE-488 (16 MHz)']
    return True, f'SCL low/high >= {lo}/{hi} us in IEEE-488 mode, within the PCF8583 spec'


@test(18, 'ATtiny: IO_IEC write does not fall into the next register')
def t_tiny_break():
    fn = function('lcd-i2c-pwm-io/main.c', 'usiTwiWriteReg')
    # Today the case after IO_IEC is "default: break", so the missing break
    # has no visible effect yet. Simulate the next register someone adds
    # (a new case before default) and check that an IO_IEC write doesn't
    # also land in it.
    if 'default:' not in fn:
        raise Skip('usiTwiWriteReg() has no default label')
    fn = fn.replace('default:', 'case NEW_REGISTER: new_register = value; break;\n      default:', 1)
    code = PRELUDE + r"""
#define PWM_CONTRAST 1
#define PWM_BRIGHTNESS 2
#define IO_IEC 3
#define NEW_REGISTER 4
static uint8_t OCR0B, OCR1B, io, new_register;
static void set_io(uint8_t v) { io = v; }
""" + fn + r"""
int main(void) {
  usiTwiWriteReg(IO_IEC, 1);
  CHECK(io == 1, "IO_IEC write lost");
  CHECK(new_register == 0, "a write to IO_IEC also changed the next register (missing break)");
  printf("an IO_IEC write only changes IO_IEC\n");
  return 0;
}
"""
    return expect(*run_c(code))

# ----------------------------------------------------------------- runner


def run_all():
    results = []
    for commit, title, fn in TESTS:
        try:
            ok, msg = fn()
            status = ok if ok == 'INFO' else ('PASS' if ok else 'FAIL')
        except Skip as e:
            status, msg = 'SKIP', str(e)
        except Exception as e:  # harness problem, show it
            status, msg = 'ERROR', str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            if VERBOSE:
                print(e, file=sys.stderr)
        results.append((commit, title, status, msg))
    return results


def main():
    global ROOT, VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--root', default=str(ROOT), help='NODISKEMU source tree to test')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    ROOT, VERBOSE = Path(a.root).resolve(), a.verbose
    results = run_all()
    if a.json:
        import json
        print(json.dumps(results))
        return 0
    print(f'Testing {ROOT}\n')
    for commit, title, status, msg in results:
        print(f'{status:5}  #{str(commit):<3} {title}\n          {msg}')
    n_fail = sum(r[2] in ('FAIL', 'ERROR') for r in results)
    n_test = sum(r[2] != 'INFO' for r in results)
    print(f'\n{n_test - n_fail} of {n_test} passed')
    return 1 if n_fail else 0


if __name__ == '__main__':
    sys.exit(main())
