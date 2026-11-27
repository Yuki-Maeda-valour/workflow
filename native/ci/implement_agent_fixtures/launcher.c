/*
 * R4X-1 の起動の包み(check_implement_agent.py が cc で組む)。旧版と Rust を同じ起動の状態にする。
 *
 *   launcher [--fsize <バイト>] [--core0] [--ignore <番号>]... [--block <番号>]... [--close <fd>]...
 *            [--umask <8進>] [--rmdir <パス>] [--env-file <パス>] -- <パス> <argv0> <引数>...
 *
 * - `--fsize`: RLIMIT_FSIZE の軟らかい上限をその値にする(硬い上限は変えない。観測用の包みが記録を書く間だけ
 *   軟らかい上限を上げられるように)。
 * - `--core0`: RLIMIT_CORE を 0 にする(core のファイルを作らない。2026-11-12 00:28・00:42 の決定)。
 * - `--ignore`: その信号を無視にしてから exec する(起動時に無視されていた信号)。
 * - `--block`: その信号を止めてから exec する。
 * - `--close`: その fd を閉じてから exec する(起動時に閉じた標準の fd)。
 * - `--rmdir`: exec の前にそのフォルダを消す(起動時の cwd が消えた起動。cwd はそのフォルダにしておく)。
 * - `--env-file`: NUL で区切った `名前=値` の並びを、並びのまま(重複・名前の無い項目も)環境にする。
 *   無ければ受け継いだ環境のまま。
 * - `<パス>` を execve で起動し、argv[0] を `<argv0>` にする。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#define environ (*_NSGetEnviron())
#else
extern char **environ;
#endif

static void die(const char *what) {
    fprintf(stderr, "launcher: %s: %s\n", what, strerror(errno));
    _exit(125);
}

static char **read_env(const char *path) {
    FILE *file = fopen(path, "rb");
    if (!file) die("env-file");
    size_t cap = 1 << 16, len = 0;
    char *data = malloc(cap);
    if (!data) die("malloc");
    size_t n;
    while ((n = fread(data + len, 1, cap - len, file)) > 0) {
        len += n;
        if (len == cap) {
            cap *= 2;
            data = realloc(data, cap);
            if (!data) die("realloc");
        }
    }
    fclose(file);
    size_t count = 0;
    for (size_t k = 0; k < len; k++)
        if (data[k] == '\0') count++;
    char **env = calloc(count + 1, sizeof *env);
    if (!env) die("calloc");
    size_t start = 0, at = 0;
    for (size_t k = 0; k < len; k++) {
        if (data[k] == '\0') {
            env[at++] = data + start;
            start = k + 1;
        }
    }
    env[at] = NULL;
    return env;
}

int main(int argc, char **argv) {
    int i = 1;
    char **child_env = environ;
    sigset_t block;
    sigemptyset(&block);
    const char *remove_dir = NULL;
    int closes[8];
    int nclose = 0;
    for (; i < argc; i++) {
        if (strcmp(argv[i], "--") == 0) {
            i++;
            break;
        }
        if (strcmp(argv[i], "--core0") != 0 && i + 1 >= argc) {
            errno = EINVAL;
            die("missing value");
        }
        if (strcmp(argv[i], "--fsize") == 0) {
            struct rlimit limit;
            if (getrlimit(RLIMIT_FSIZE, &limit) != 0) die("getrlimit fsize");
            limit.rlim_cur = (rlim_t)strtoull(argv[++i], NULL, 10);
            if (setrlimit(RLIMIT_FSIZE, &limit) != 0) die("setrlimit fsize");
        } else if (strcmp(argv[i], "--core0") == 0) {
            struct rlimit limit;
            limit.rlim_cur = limit.rlim_max = 0;
            if (setrlimit(RLIMIT_CORE, &limit) != 0) die("setrlimit core");
            continue; /* 値を取らない選択肢。 */
        } else if (strcmp(argv[i], "--ignore") == 0) {
            int number = atoi(argv[++i]);
            struct sigaction action;
            memset(&action, 0, sizeof action);
            action.sa_handler = SIG_IGN;
            sigemptyset(&action.sa_mask);
            if (sigaction(number, &action, NULL) != 0) die("sigaction");
        } else if (strcmp(argv[i], "--block") == 0) {
            sigaddset(&block, atoi(argv[++i]));
        } else if (strcmp(argv[i], "--close") == 0) {
            if (nclose < 8) closes[nclose++] = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--rmdir") == 0) {
            remove_dir = argv[++i];
        } else if (strcmp(argv[i], "--env-file") == 0) {
            child_env = read_env(argv[++i]);
        } else if (strcmp(argv[i], "--umask") == 0) {
            umask((mode_t)strtoul(argv[++i], NULL, 8));
        } else {
            errno = EINVAL;
            die(argv[i]);
        }
    }
    if (i + 1 >= argc) {
        errno = EINVAL;
        die("missing program");
    }
    const char *path = argv[i];
    char **child = &argv[i + 1];
    if (remove_dir && rmdir(remove_dir) != 0) die("rmdir");
    if (sigprocmask(SIG_SETMASK, &block, NULL) != 0) die("sigprocmask");
    for (int k = 0; k < nclose; k++) close(closes[k]);
    execve(path, child, child_env);
    die("execve");
    return 125;
}
