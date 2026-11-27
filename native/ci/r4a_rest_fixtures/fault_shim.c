// 旧 Python の標準出力・標準エラーへの write(2) に故障を差し込む共有ライブラリ。
// 比較器がビルドして使う。Linux は LD_PRELOAD、Mac は `cc -dynamiclib` と DYLD_INSERT_LIBRARIES(__interpose の表)。Rust 側の同じ規則は専用 example の書込みの差替え。
//
// R4A_FAULT="<fd>:<mode>:<n>"  <fd> は 1 か 2。故障の規則は書込みの番号ではなく出来事で決める。
//   budget:N      合計 N バイトを通した後は 1 バイトも通さない(ENOSPC)。N を越える書込みは N までで切る。
//   cut-first:K   その fd への最初の書込みだけを K バイトで切る(短い書込み)。残りの書込みは普通に通る。
//   short-fail:K  最初の書込みを K バイトで切り、その後の書込みはすべて ENOSPC で失敗する。
//   fail-first:E  最初の書込みだけ errno E(既定 ENOSPC=28。n に E を書く)で失敗し、その後は普通に通る。
//   fail-nth:N    N 番目の書込みだけ ENOSPC で失敗する(再現の回数を固定する検査専用)。
//   fail-first-and-nth:N  1 番目と N 番目の書込みが ENOSPC で失敗する(最初の失敗の後の Traceback の途中の失敗を作る)。
//   fail-second:E 2 番目の書込みだけ errno E で失敗する。
// R4A_RACE="<event>:<action>:<path>[:<arg>]"  permlog の読取りの途中に、別の処理が起こした変更(競合)を、決まった出来事で差し込む。
//   event: before-parent-open:<名前>  その名前の親ディレクトリを開く直前 / before-leaf-open  末端を開く直前 /
//          before-first-read  最初の read の直前 / after-first-read  最初の read の直後。
//   action: truncate:<path>:<長さ>  append:<path>:<文字列>  rewrite:<path>:<同じ長さの文字列>(mtime も変える) /
//           swap:<path>:<target>(path を target を指す symlink に差し替える)。
// R4A_CLOSE_FAIL=<errno>  O_NOFOLLOW と O_NONBLOCK で開いたファイル(permlog)の close を、実際に閉じた後に errno で失敗させる。
// R4A_CALLS=<path>  終了時に、差込みが見た呼び出しの要点(write の fd ごとの回数とバイト数・read・open・close の回数)を 1 行で追記する(診断用)。
// R4A_TRACE=<path>  その fd への書込み要求の中身を、4 バイトの長さ(little endian)+中身の列で書き出す。
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __APPLE__
// Mac: 差込み関数は別名で定義し、__interpose の表で本物と差し替える(この図書館自身の参照は本物へ向かう)。
#define HOOK(name) r4a_##name
#define INTERPOSE(name)                                                                              \
  __attribute__((used)) static struct { const void *replacement; const void *replacee; } r4a_interpose_##name \
      __attribute__((section("__DATA,__interpose"))) = {(const void *)(unsigned long)&r4a_##name, (const void *)(unsigned long)&name}
static void *next_symbol(const char *name) {
  if (!strcmp(name, "write")) return (void *)&write;
  if (!strcmp(name, "read")) return (void *)&read;
  if (!strcmp(name, "close")) return (void *)&close;
  if (!strcmp(name, "open")) return (void *)&open;
  if (!strcmp(name, "openat")) return (void *)&openat;
  return NULL;
}
#else
#define HOOK(name) name
#define INTERPOSE(name)
static void *next_symbol(const char *name) { return dlsym(RTLD_NEXT, name); }
#endif

static ssize_t (*original_write)(int, const void *, size_t);
static int target_fd = 0, trace_fd = -1;
static char mode[32];
static size_t limit = 0, accepted = 0;
static unsigned long sequence = 0;
// 診断用の呼び出しの記録(R4A_CALLS)。write は fd 0〜2 とそれ以外を分ける。
static unsigned long calls_write[4], calls_write_bytes[4], calls_read, calls_open, calls_close;
static const char *calls_path = NULL;
static int failing = 0;

__attribute__((constructor)) static void setup(void) {
  int saved = errno;
  original_write = next_symbol("write");
  const char *spec = getenv("R4A_FAULT");
  if (spec) {
    char name[32];
    unsigned long n = 0;
    int fd = 0;
    if (sscanf(spec, "%d:%31[a-z-]:%lu", &fd, name, &n) == 3) {
      target_fd = fd;
      strncpy(mode, name, sizeof mode - 1);
      limit = n;
    }
  }
  calls_path = getenv("R4A_CALLS");
  const char *trace = getenv("R4A_TRACE");
  if (trace) trace_fd = open(trace, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  errno = saved;
}

ssize_t HOOK(write)(int fd, const void *data, size_t size) {
  if (!original_write) original_write = next_symbol("write");
  if (calls_path) {
    int slot = (fd >= 0 && fd <= 2) ? fd : 3;
    calls_write[slot]++;
    calls_write_bytes[slot] += size;
  }
  if (fd != target_fd || target_fd == 0) return original_write(fd, data, size);
  unsigned long index = ++sequence;
  if (trace_fd >= 0) {
    uint32_t length = (uint32_t)size;
    original_write(trace_fd, &length, 4);
    original_write(trace_fd, data, size);
  }
  if (!strcmp(mode, "budget")) {
    size_t remaining = limit > accepted ? limit - accepted : 0;
    if (remaining == 0) {
      errno = ENOSPC;
      return -1;
    }
    size_t count = size < remaining ? size : remaining;
    ssize_t result = original_write(fd, data, count);
    if (result > 0) accepted += (size_t)result;
    return result;
  }
  if (!strcmp(mode, "fail-nth") || !strcmp(mode, "fail-first-and-nth")) {
    if (index == limit || (index == 1 && !strcmp(mode, "fail-first-and-nth"))) {
      errno = ENOSPC;
      return -1;
    }
    return original_write(fd, data, size);
  }
  if (!strcmp(mode, "fail-first") || !strcmp(mode, "fail-second")) {
    unsigned long which = !strcmp(mode, "fail-first") ? 1 : 2;
    if (index == which) {
      errno = limit ? (int)limit : ENOSPC;
      return -1;
    }
    return original_write(fd, data, size);
  }
  if (!strcmp(mode, "cut-first")) {
    size_t count = (index == 1 && size > limit) ? limit : size;
    return original_write(fd, data, count);
  }
  if (!strcmp(mode, "short-fail")) {
    if (failing) {
      errno = ENOSPC;
      return -1;
    }
    if (index == 1 && size > limit) {
      failing = 1;
      return original_write(fd, data, limit);
    }
    return original_write(fd, data, size);
  }
  return original_write(fd, data, size);
}

// ---- 競合の差込み(permlog)。
static char race_event[64], race_action[16], race_path[1024], race_arg[1024];
static int race_ready = 0, race_done = 0, race_fd = -1, race_reads = 0;

static void race_setup(void) {
  if (race_ready) return;
  race_ready = 1;
  const char *spec = getenv("R4A_RACE");
  if (!spec) return;
  char buffer[4096];
  strncpy(buffer, spec, sizeof buffer - 1);
  buffer[sizeof buffer - 1] = 0;
  char *fields[6];
  int count = 0;
  for (char *cursor = strtok(buffer, ":"); cursor && count < 6; cursor = strtok(NULL, ":")) fields[count++] = cursor;
  int shift = (count > 0 && !strcmp(fields[0], "before-parent-open")) ? 1 : 0;
  if (count < 3 + shift) return;
  snprintf(race_event, sizeof race_event, "%s%s%s", fields[0], shift ? ":" : "", shift ? fields[1] : "");
  strncpy(race_action, fields[1 + shift], sizeof race_action - 1);
  strncpy(race_path, fields[2 + shift], sizeof race_path - 1);
  if (count > 3 + shift) strncpy(race_arg, fields[3 + shift], sizeof race_arg - 1);
}

static void race_run(void) {
  if (race_done) return;
  race_done = 1;
  if (!strcmp(race_action, "truncate")) {
    if (truncate(race_path, atol(race_arg)) != 0) return;
  } else if (!strcmp(race_action, "append") || !strcmp(race_action, "rewrite")) {
    int append = !strcmp(race_action, "append");
    int fd = open(race_path, O_WRONLY | (append ? O_APPEND : 0));
    if (fd < 0) return;
    ssize_t n = write(fd, race_arg, strlen(race_arg));
    (void)n;
    close(fd);
    if (!append) {
      struct timeval times[2];
      gettimeofday(&times[0], NULL);
      times[1] = times[0];
      times[1].tv_sec += 5;  // mtime を確実に違う値にする(粗い時計で同じ値になる偶然を避ける)
      utimes(race_path, times);
    }
  } else if (!strcmp(race_action, "swap")) {
    char old[1100];
    snprintf(old, sizeof old, "%s.orig", race_path);
    rename(race_path, old);
    if (symlink(race_arg, race_path) != 0) return;
  }
}

static void race_on_open(int flags, const char *path) {
  race_setup();
  if (!race_event[0] || race_done) return;
  if ((flags & O_DIRECTORY) && (flags & O_NOFOLLOW)) {
    char expected[96];
    snprintf(expected, sizeof expected, "before-parent-open:%s", path);
    if (!strcmp(race_event, expected)) race_run();
  } else if ((flags & O_NOFOLLOW) && (flags & O_NONBLOCK) && !strcmp(race_event, "before-leaf-open")) {
    race_run();
  }
}

ssize_t HOOK(read)(int fd, void *data, size_t size) {
  static ssize_t (*real_read)(int, void *, size_t);
  if (!real_read) real_read = next_symbol("read");
  if (calls_path) calls_read++;
  if (race_fd < 0 || fd != race_fd) return real_read(fd, data, size);
  int first = (++race_reads == 1);
  if (first && !strcmp(race_event, "before-first-read")) race_run();
  ssize_t result = real_read(fd, data, size);
  if (first && !strcmp(race_event, "after-first-read")) {
    int saved = errno;
    race_run();
    errno = saved;
  }
  return result;
}

// permlog の close の失敗(旧版の `os.close` が OSError になる条件)。
static int close_fault_fd = -1;

static int note_open(int fd, int flags) {
  if (fd >= 0 && (flags & O_NOFOLLOW) && (flags & O_NONBLOCK)) {
    if (getenv("R4A_CLOSE_FAIL")) close_fault_fd = fd;
    if (getenv("R4A_RACE")) race_fd = fd;
  }
  return fd;
}

static int traced_openat(const char *symbol, int dirfd, const char *path, int flags, mode_t mode) {
  static int (*real_openat)(int, const char *, int, ...);
  static int (*real_openat64)(int, const char *, int, ...);
  int (**real)(int, const char *, int, ...) = strcmp(symbol, "openat") ? &real_openat64 : &real_openat;
  if (!*real) *real = next_symbol(strcmp(symbol, "openat") ? "openat" : "openat");
  if (calls_path) calls_open++;
  race_on_open(flags, path);
  return note_open((*real)(dirfd, path, flags, mode), flags);
}

#ifdef O_TMPFILE
#define OPEN_CREATE_FLAGS (O_CREAT | O_TMPFILE)
#else
#define OPEN_CREATE_FLAGS O_CREAT
#endif

#define OPENAT_BODY(name)                      \
  mode_t mode = 0;                             \
  if (flags & OPEN_CREATE_FLAGS) {             \
    va_list args;                              \
    va_start(args, flags);                     \
    mode = va_arg(args, mode_t);               \
    va_end(args);                              \
  }                                            \
  return traced_openat(name, dirfd, path, flags, mode);

int HOOK(openat)(int dirfd, const char *path, int flags, ...) { OPENAT_BODY("openat") }
#ifndef __APPLE__
int openat64(int dirfd, const char *path, int flags, ...) {
  static int (*real_openat64)(int, const char *, int, ...);
  if (!real_openat64) real_openat64 = dlsym(RTLD_NEXT, "openat64");
  mode_t mode = 0;
  if (flags & OPEN_CREATE_FLAGS) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, mode_t);
    va_end(args);
  }
  race_on_open(flags, path);
  return note_open(real_openat64(dirfd, path, flags, mode), flags);
}
#else
// Mac の Python は dir_fd が無い open を `open` で呼ぶ。
int HOOK(open)(const char *path, int flags, ...) {
  static int (*real_open)(const char *, int, ...);
  if (!real_open) real_open = next_symbol("open");
  mode_t mode = 0;
  if (flags & OPEN_CREATE_FLAGS) {
    va_list args;
    va_start(args, flags);
    mode = va_arg(args, mode_t);
    va_end(args);
  }
  race_on_open(flags, path);
  return note_open(real_open(path, flags, mode), flags);
}
INTERPOSE(open);
#endif

int HOOK(close)(int fd) {
  static int (*real_close)(int);
  if (!real_close) real_close = next_symbol("close");
  if (calls_path) calls_close++;
  int result = real_close(fd);
  if (fd >= 0 && fd == close_fault_fd) {
    close_fault_fd = -1;
    errno = atoi(getenv("R4A_CLOSE_FAIL"));
    return -1;
  }
  return result;
}

INTERPOSE(write);
INTERPOSE(read);
INTERPOSE(close);
INTERPOSE(openat);

// 診断用: 終了時に、見えた呼び出しの要点を追記する(write は fd 0・1・2・その他の「回数/バイト数」)。
__attribute__((destructor)) static void dump_calls(void) {
  if (!calls_path || !original_write) return;
  int saved = errno;
  FILE *out = fopen(calls_path, "a");
  if (out) {
    fprintf(out, "write fd0=%lu/%lu fd1=%lu/%lu fd2=%lu/%lu other=%lu/%lu read=%lu open=%lu close=%lu\n", calls_write[0],
            calls_write_bytes[0], calls_write[1], calls_write_bytes[1], calls_write[2], calls_write_bytes[2], calls_write[3],
            calls_write_bytes[3], calls_read, calls_open, calls_close);
    fclose(out);
  }
  errno = saved;
}
