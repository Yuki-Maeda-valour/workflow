/*
 * R4X-1 の観測用の包み(check_implement_agent.py が cc で組む)。
 *
 * PATH の先頭のフォルダに grep・dirname・basename・mkdir の名前で置く。起動されると、
 * 自分の実行ファイルのフォルダの 1 つ上の conf を読み、起動の記録(argv・environ・fd 0〜2 の種類・
 * 信号の扱いと止め・cwd・pid と ppid)を記録のフォルダに書く。
 *
 * - conf の行: `real=<本物のパス>`(名前ごとに `real.<名前>=`)・`log=<記録のフォルダ>`・
 *   `stop=<止める道具の名前か none>`・`stopdir=<印と FIFO のフォルダ>`・`mkdir=<kill|wait>`。
 * - 止める道具なら、FIFO を O_RDWR で開いてから印(`stop-<地点>`)を書き、read(2) で 1 バイトの合図を
 *   待つ(合図を待つための子を作らない)。
 * - grep・dirname・basename は、本物を argv[0] のまま execve で起動する(環境は変えない)。
 * - mkdir: `mkdir=kill` なら、記録の後に親(bash)を SIGKILL で止めて 1 で終わる。`mkdir=wait` なら、
 *   1 回目は合図の後に何も作らず 1 で終わる。2 回目は回数を記録して親を SIGKILL で止める。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#include <crt_externs.h>
#define environ (*_NSGetEnviron())
#else
extern char **environ;
#endif

static char conf_real[PATH_MAX];
static char conf_log[PATH_MAX];
static char conf_stop[64];
static char conf_stopdir[PATH_MAX];
static char conf_mkdir[16];

static void fail(const char *what) {
    char text[512];
    int n = snprintf(text, sizeof text, "observer: %s: %s\n", what, strerror(errno));
    if (n > 0) {
        ssize_t ignored = write(2, text, (size_t)n);
        (void)ignored;
    }
    _exit(125);
}

static int exe_path(char *out, size_t size) {
#ifdef __APPLE__
    uint32_t length = (uint32_t)size;
    char raw[PATH_MAX];
    uint32_t raw_length = sizeof raw;
    if (_NSGetExecutablePath(raw, &raw_length) != 0) return -1;
    (void)length;
    if (strlen(raw) + 1 > size) return -1;
    strcpy(out, raw);
    return 0;
#else
    ssize_t n = readlink("/proc/self/exe", out, size - 1);
    if (n < 0) return -1;
    out[n] = '\0';
    return 0;
#endif
}

static void read_conf(const char *tool) {
    char exe[PATH_MAX];
    if (exe_path(exe, sizeof exe) != 0) fail("exe");
    char *slash = strrchr(exe, '/');
    if (!slash) fail("exe slash");
    *slash = '\0';
    slash = strrchr(exe, '/');
    if (!slash) fail("exe parent");
    *slash = '\0';
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/conf", exe);
    FILE *conf = fopen(path, "r");
    if (!conf) fail("conf");
    char line[PATH_MAX + 64];
    char key[64];
    snprintf(key, sizeof key, "real.%s=", tool);
    while (fgets(line, sizeof line, conf)) {
        size_t n = strlen(line);
        if (n && line[n - 1] == '\n') line[n - 1] = '\0';
        if (strncmp(line, key, strlen(key)) == 0) snprintf(conf_real, sizeof conf_real, "%s", line + strlen(key));
        else if (strncmp(line, "log=", 4) == 0) snprintf(conf_log, sizeof conf_log, "%s", line + 4);
        else if (strncmp(line, "stop=", 5) == 0) snprintf(conf_stop, sizeof conf_stop, "%s", line + 5);
        else if (strncmp(line, "stopdir=", 8) == 0) snprintf(conf_stopdir, sizeof conf_stopdir, "%s", line + 8);
        else if (strncmp(line, "mkdir=", 6) == 0) snprintf(conf_mkdir, sizeof conf_mkdir, "%s", line + 6);
    }
    fclose(conf);
    if (!conf_log[0]) fail("conf log");
}

/* 記録のファイルを O_EXCL で作る(<log>/<tool>.<n>)。n を返す。 */
static int open_record(const char *tool, int *fd_out) {
    for (int n = 1; n < 1000; n++) {
        char path[PATH_MAX];
        snprintf(path, sizeof path, "%s/%s.%d", conf_log, tool, n);
        int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (fd >= 0) {
            *fd_out = fd;
            return n;
        }
        if (errno != EEXIST) fail("record");
    }
    errno = EEXIST;
    fail("record count");
    return 0;
}

static void put(int fd, const char *data, size_t n) {
    while (n) {
        ssize_t w = write(fd, data, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            fail("write record");
        }
        data += w;
        n -= (size_t)w;
    }
}

static void put_str(int fd, const char *s) { put(fd, s, strlen(s)); }

static void put_field(int fd, const char *name, const char *value, size_t n) {
    char head[64];
    int h = snprintf(head, sizeof head, "%s %zu\n", name, n);
    put(fd, head, (size_t)h);
    put(fd, value, n);
    put(fd, "\n", 1);
}

static const char *fd_kind(int fd) {
    struct stat info;
    if (fstat(fd, &info) != 0) return "closed";
    if (S_ISREG(info.st_mode)) return "file";
    if (S_ISDIR(info.st_mode)) return "dir";
    if (S_ISCHR(info.st_mode)) return "char";
    if (S_ISBLK(info.st_mode)) return "block";
    if (S_ISFIFO(info.st_mode)) return "fifo";
    if (S_ISSOCK(info.st_mode)) return "socket";
    return "other";
}

static void record(int fd) {
    /* 信号の扱い(無視の集合)と止め。 */
    unsigned long long ignored = 0, blocked = 0;
    sigset_t mask;
    sigemptyset(&mask);
    sigprocmask(SIG_BLOCK, NULL, &mask);
    for (int s = 1; s < 32; s++) {
        struct sigaction old;
        if (s == SIGKILL || s == SIGSTOP) continue;
        if (sigaction(s, NULL, &old) == 0 && old.sa_handler == SIG_IGN) ignored |= 1ULL << s;
        if (sigismember(&mask, s) == 1) blocked |= 1ULL << s;
    }
    char text[256];
    int n = snprintf(text, sizeof text, "ignored %llu\nblocked %llu\n", ignored, blocked);
    put(fd, text, (size_t)n);
    for (int f = 0; f <= 2; f++) {
        n = snprintf(text, sizeof text, "fd%d %s\n", f, fd_kind(f));
        put(fd, text, (size_t)n);
    }
    char cwd[PATH_MAX];
    if (getcwd(cwd, sizeof cwd)) put_field(fd, "cwd", cwd, strlen(cwd));
    else put_field(fd, "cwd", "", 0);
    n = snprintf(text, sizeof text, "pid %ld\nppid %ld\n", (long)getpid(), (long)getppid());
    put(fd, text, (size_t)n);
}

static void record_args_env(int fd, int argc, char **argv) {
    char text[64];
    int n = snprintf(text, sizeof text, "argc %d\n", argc);
    put(fd, text, (size_t)n);
    for (int i = 0; i < argc; i++) put_field(fd, "arg", argv[i], strlen(argv[i]));
    int count = 0;
    for (char **e = environ; *e; e++) count++;
    n = snprintf(text, sizeof text, "envc %d\n", count);
    put(fd, text, (size_t)n);
    for (char **e = environ; *e; e++) put_field(fd, "env", *e, strlen(*e));
}

/* 止める: FIFO を O_RDWR で開き、印を書き、1 バイトの合図を待つ。 */
static void stop_here(const char *point) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/fifo", conf_stopdir);
    int fifo = open(path, O_RDWR | O_CLOEXEC);
    if (fifo < 0) fail("fifo");
    char mark[PATH_MAX];
    snprintf(mark, sizeof mark, "%s/stop-%s", conf_stopdir, point);
    int fd = open(mark, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0) fail("mark");
    char text[64];
    int n = snprintf(text, sizeof text, "observer %ld\n", (long)getpid());
    put(fd, text, (size_t)n);
    close(fd);
    char byte;
    for (;;) {
        ssize_t r = read(fifo, &byte, 1);
        if (r == 1) break;
        if (r < 0 && errno == EINTR) continue;
        fail("fifo read");
    }
    close(fifo);
}

int main(int argc, char **argv) {
    /* 記録を書く間だけ RLIMIT_FSIZE の軟らかい上限を上げ、本物を起動する前に戻す(bash が渡した上限のまま)。 */
    struct rlimit original;
    int have_limit = getrlimit(RLIMIT_FSIZE, &original) == 0;
    if (have_limit) {
        struct rlimit raised = original;
        raised.rlim_cur = original.rlim_max;
        setrlimit(RLIMIT_FSIZE, &raised);
    }
    char exe[PATH_MAX];
    if (exe_path(exe, sizeof exe) != 0) fail("exe");
    const char *tool = strrchr(exe, '/');
    tool = tool ? tool + 1 : exe;
    read_conf(tool);
    int fd = -1;
    int count = open_record(tool, &fd);
    record_args_env(fd, argc, argv);
    record(fd);
    close(fd);
    int is_mkdir = strcmp(tool, "mkdir") == 0;
    if (is_mkdir) {
        if (strcmp(conf_mkdir, "wait") == 0 && count == 1 && strcmp(conf_stop, "boundary") == 0) {
            stop_here("boundary");
            return 1;
        }
        kill(getppid(), SIGKILL);
        return 1;
    }
    if (strcmp(conf_stop, tool) == 0 && count == 1) stop_here(tool);
    if (!conf_real[0]) {
        errno = ENOENT;
        fail("conf real");
    }
    if (have_limit) setrlimit(RLIMIT_FSIZE, &original);
    execve(conf_real, argv, environ);
    fail("execve");
    return 125;
}
