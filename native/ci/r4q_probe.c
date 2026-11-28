/*
 * R4Q(#494)の比較器の補助。比較器が `cc` で作る(製品には入れない)。
 *
 * 品質コマンドの代わり(モード state):
 *   r4q_probe state <状態のファイル> [選択肢…]
 *   自分の見る状態(pid・ppid・pgid・sid・作業フォルダ・env の並び・開いた fd・stdin の開き方と種類・信号の mask と
 *   無視と捕捉の設定・親の argv・Linux では親の /proc/<ppid>/status の SigBlk と SigIgn)を JSON で状態の
 *   ファイルへ書く(一時のファイルへ書いてから rename する)。
 *   選択肢(書いた順に動く):
 *     --write-stdout <n>   stdout へ 'o' を n バイト書く(状態の前)
 *     --write-stderr <n>   stderr へ 'e' を n バイト書く(状態の前)
 *     --term-log <file>    TERM を受けるたびに、受けた時刻(CLOCK_REALTIME の struct timespec。16 バイト)を足す
 *                          (数えるのと、片付けの経過の下限を確かめるため)
 *     --ignore-term        TERM で終わらない(数えるだけ)
 *     --started <file>     状態を書いた後に「始めた」印を作る
 *     --grandchild <形>    孫を作る: double(子が孫を作って終わる)・setsid(孫が setsid する)・
 *                          hold(孫が stdout を開いたまま眠る)・writer(setsid した孫が stdout へ書き続ける)
 *     --grandchild-pid <file>  孫の pid を書く
 *     --sleep <秒>         眠る(信号で起きても残りを眠る)
 *     --exit <n>           終了値
 *     --kill-self <番号>   最後に自分へ信号を送る
 *     --mark <印>          何もしない(ps で見つけるための印)
 *
 * subreaper の補助(モード hold。Linux だけ。設計の Task 3.1 の別のモード):
 *   r4q_probe hold <保持の上限の秒> <起動した比較器の pid> <結果のファイル> -- <コマンド…>
 *   PR_SET_CHILD_SUBREAPER を設定し、PR_SET_PDEATHSIG を SIGKILL にし、getppid() が比較器の pid でなければ
 *   自分で回収して終わる。コマンド(比べる親。stdin は /dev/null)を起動し、その pid だけを waitpid で待つ。
 *   コマンドの終了値(waitpid の status)を結果のファイルへ書く。引き取った子は、stdin(比較器が持つパイプ)が
 *   閉じるか上限を過ぎるまで回収せずに残し、その後に WNOHANG で回収して終わる(5 秒まで。生きた子が残れば、
 *   補助の死後に init か上位の subreaper へ移る)。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif
#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

extern char **environ;

static const char *term_log = NULL;
static volatile sig_atomic_t terms = 0;

static void on_term(int number) {
    (void)number;
    int saved = errno;
    terms++;
    if (term_log != NULL) {
        struct timespec now;
        clock_gettime(CLOCK_REALTIME, &now);
        int64_t record[2] = {(int64_t)now.tv_sec, (int64_t)now.tv_nsec};
        int fd = open(term_log, O_WRONLY | O_APPEND | O_CREAT, 0600);
        if (fd >= 0) {
            ssize_t ignored = write(fd, record, sizeof record);
            (void)ignored;
            close(fd);
        }
    }
    errno = saved;
}

static void die(const char *what) {
    fprintf(stderr, "r4q_probe: %s: %s\n", what, strerror(errno));
    exit(125);
}

typedef struct {
    char *data;
    size_t len;
    size_t cap;
} buf;

static void put(buf *b, const char *text, size_t len) {
    if (b->len + len + 1 > b->cap) {
        size_t cap = (b->cap + len + 1) * 2;
        char *data = realloc(b->data, cap);
        if (data == NULL) die("realloc");
        b->data = data;
        b->cap = cap;
    }
    memcpy(b->data + b->len, text, len);
    b->len += len;
    b->data[b->len] = '\0';
}

static void puts_(buf *b, const char *text) { put(b, text, strlen(text)); }

static void put_hex(buf *b, const unsigned char *data, size_t len) {
    static const char digits[] = "0123456789abcdef";
    puts_(b, "\"");
    for (size_t i = 0; i < len; i++) {
        char pair[2] = {digits[data[i] >> 4], digits[data[i] & 15]};
        put(b, pair, 2);
    }
    puts_(b, "\"");
}

static void put_int(buf *b, long long value) {
    char text[32];
    snprintf(text, sizeof text, "%lld", value);
    puts_(b, text);
}

/* 親の argv(NUL 区切りの並び)。読めなければ len 0。 */
static unsigned char *parent_argv(pid_t ppid, size_t *len) {
    *len = 0;
#ifdef __linux__
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/cmdline", (int)ppid);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return NULL;
    size_t cap = 1 << 16;
    unsigned char *data = malloc(cap);
    if (data == NULL) die("malloc");
    for (;;) {
        ssize_t got = read(fd, data + *len, cap - *len);
        if (got <= 0) break;
        *len += (size_t)got;
        if (*len == cap) {
            cap *= 2;
            data = realloc(data, cap);
            if (data == NULL) die("realloc");
        }
    }
    close(fd);
    return data;
#elif defined(__APPLE__)
    int mib[3] = {CTL_KERN, KERN_PROCARGS2, (int)ppid};
    size_t size = 0;
    if (sysctl(mib, 3, NULL, &size, NULL, 0) != 0 || size < sizeof(int)) return NULL;
    unsigned char *raw = malloc(size);
    if (raw == NULL) die("malloc");
    if (sysctl(mib, 3, raw, &size, NULL, 0) != 0) {
        free(raw);
        return NULL;
    }
    int argc;
    memcpy(&argc, raw, sizeof argc);
    size_t at = sizeof argc;
    while (at < size && raw[at] != '\0') at++; /* 実行ファイルのパス */
    while (at < size && raw[at] == '\0') at++;
    size_t start = at;
    int seen = 0;
    while (at < size && seen < argc) {
        if (raw[at] == '\0') seen++;
        at++;
    }
    unsigned char *data = malloc(at - start + 1);
    if (data == NULL) die("malloc");
    memcpy(data, raw + start, at - start);
    *len = at - start;
    free(raw);
    return data;
#else
    (void)ppid;
    return NULL;
#endif
}

/* 親の /proc/<ppid>/status の行(例 "SigBlk")の値を out へ。読めなければ 0(Mac も 0)。 */
static int read_parent_status(pid_t ppid, const char *name, char *out, size_t size) {
#ifdef __linux__
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/status", (int)ppid);
    FILE *stream = fopen(path, "re");
    if (stream == NULL) return 0;
    char line[512];
    size_t len = strlen(name);
    int found = 0;
    while (fgets(line, sizeof line, stream) != NULL) {
        if (strncmp(line, name, len) == 0 && line[len] == ':') {
            char *value = line + len + 1;
            while (*value == ' ' || *value == '\t') value++;
            value[strcspn(value, "\n")] = '\0';
            snprintf(out, size, "%s", value);
            found = 1;
            break;
        }
    }
    fclose(stream);
    return found;
#else
    (void)ppid;
    (void)name;
    (void)out;
    (void)size;
    return 0;
#endif
}

/* 信号 1〜31 のうち止めている数(SigBlk の 16 進)。 */
static int blocked_count(const char *hex) {
    unsigned long long mask = strtoull(hex, NULL, 16);
    int count = 0;
    for (int bit = 0; bit < 31; bit++) {
        if (mask & (1ULL << bit)) count++;
    }
    return count;
}

/* 親の SigBlk・SigIgn を JSON の値で書く。親は子の起動の間だけ全部の信号を止める(旧版の vfork の前と、Rust の
 * fork の前)。子の exec の後に親が mask を戻すまでの間に読むと、起動の途中の値になるので、ほとんど全部
 * (1〜31 のうち 29 以上)を止めた値の間は 10 ミリ秒ごとに 2 秒まで読み直す(戻さない誤りは、2 秒後の値で比べて見つける)。 */
static void put_parent_status(buf *b, pid_t ppid, const char *name) {
    char value[128];
    if (!read_parent_status(ppid, name, value, sizeof value)) {
        puts_(b, "null");
        return;
    }
    if (strcmp(name, "SigBlk") == 0) {
        for (int round = 0; round < 200 && blocked_count(value) >= 29; round++) {
            struct timespec step = {0, 10000000L};
            nanosleep(&step, NULL);
            if (!read_parent_status(ppid, name, value, sizeof value)) break;
        }
    }
    puts_(b, "\"");
    puts_(b, value);
    puts_(b, "\"");
}

static void write_state(const char *path) {
    buf b = {0};
    puts_(&b, "{\"pid\": ");
    put_int(&b, getpid());
    puts_(&b, ", \"ppid\": ");
    put_int(&b, getppid());
    puts_(&b, ", \"pgid\": ");
    put_int(&b, getpgid(0));
    puts_(&b, ", \"sid\": ");
    put_int(&b, getsid(0));
    char cwd[4096];
    puts_(&b, ", \"cwd\": ");
    if (getcwd(cwd, sizeof cwd) != NULL) {
        put_hex(&b, (const unsigned char *)cwd, strlen(cwd));
    } else {
        puts_(&b, "null");
    }
    puts_(&b, ", \"env\": [");
    for (char **entry = environ; *entry != NULL; entry++) {
        if (entry != environ) puts_(&b, ", ");
        put_hex(&b, (const unsigned char *)*entry, strlen(*entry));
    }
    puts_(&b, "], \"fds\": [");
    int first = 1;
    for (int fd = 0; fd < 1024; fd++) {
        if (fcntl(fd, F_GETFD) >= 0) {
            if (!first) puts_(&b, ", ");
            first = 0;
            put_int(&b, fd);
        }
    }
    puts_(&b, "], \"stdin\": ");
    int flags = fcntl(0, F_GETFL);
    struct stat st;
    struct stat null;
    if (flags < 0 || fstat(0, &st) != 0) {
        puts_(&b, "null");
    } else {
        const char *mode = (flags & O_ACCMODE) == O_RDONLY ? "rdonly"
                           : (flags & O_ACCMODE) == O_WRONLY ? "wronly" : "rdwr";
        int is_null = stat("/dev/null", &null) == 0 && S_ISCHR(st.st_mode) && st.st_rdev == null.st_rdev;
        puts_(&b, "{\"mode\": \"");
        puts_(&b, mode);
        puts_(&b, "\", \"devnull\": ");
        puts_(&b, is_null ? "true" : "false");
        puts_(&b, "}");
    }
    sigset_t mask;
    sigprocmask(SIG_BLOCK, NULL, &mask);
    puts_(&b, ", \"blocked\": [");
    first = 1;
    for (int number = 1; number < 32; number++) {
        if (sigismember(&mask, number) == 1) {
            if (!first) puts_(&b, ", ");
            first = 0;
            put_int(&b, number);
        }
    }
    const char *kinds[2] = {"ignored", "caught"};
    for (int kind = 0; kind < 2; kind++) {
        puts_(&b, "], \"");
        puts_(&b, kinds[kind]);
        puts_(&b, "\": [");
        first = 1;
        for (int number = 1; number < 32; number++) {
            struct sigaction action;
            if (number == SIGKILL || number == SIGSTOP || sigaction(number, NULL, &action) != 0) continue;
            int ignored = action.sa_handler == SIG_IGN;
            int caught = action.sa_handler != SIG_IGN && action.sa_handler != SIG_DFL;
            if ((kind == 0 && ignored) || (kind == 1 && caught)) {
                if (!first) puts_(&b, ", ");
                first = 0;
                put_int(&b, number);
            }
        }
    }
    puts_(&b, "], \"parent_sigblk\": ");
    put_parent_status(&b, getppid(), "SigBlk");
    puts_(&b, ", \"parent_sigign\": ");
    put_parent_status(&b, getppid(), "SigIgn");
    puts_(&b, ", \"parent_argv\": ");
    size_t len;
    unsigned char *argv = parent_argv(getppid(), &len);
    if (argv == NULL) {
        puts_(&b, "null");
    } else {
        puts_(&b, "[");
        size_t start = 0;
        int count = 0;
        for (size_t at = 0; at < len; at++) {
            if (argv[at] == '\0') {
                if (count++) puts_(&b, ", ");
                put_hex(&b, argv + start, at - start);
                start = at + 1;
            }
        }
        puts_(&b, "]");
        free(argv);
    }
    puts_(&b, "}\n");
    char temporary[4200];
    snprintf(temporary, sizeof temporary, "%s.part", path);
    int fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) die("状態のファイルを作れない");
    size_t done = 0;
    while (done < b.len) {
        ssize_t got = write(fd, b.data + done, b.len - done);
        if (got < 0) {
            if (errno == EINTR) continue;
            die("状態を書けない");
        }
        done += (size_t)got;
    }
    close(fd);
    if (rename(temporary, path) != 0) die("状態のファイルを置けない");
    free(b.data);
}

static void touch(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) die("印を作れない");
    close(fd);
}

static void fill(int fd, char byte, long count) {
    char chunk[8192];
    memset(chunk, byte, sizeof chunk);
    while (count > 0) {
        size_t want = count > (long)sizeof chunk ? sizeof chunk : (size_t)count;
        ssize_t got = write(fd, chunk, want);
        if (got < 0) {
            if (errno == EINTR) continue;
            die("出力へ書けない");
        }
        count -= got;
    }
}

static void sleep_for(double seconds) {
    struct timespec end;
    clock_gettime(CLOCK_MONOTONIC, &end);
    end.tv_sec += (time_t)seconds;
    end.tv_nsec += (long)((seconds - (double)(time_t)seconds) * 1e9);
    if (end.tv_nsec >= 1000000000L) {
        end.tv_sec++;
        end.tv_nsec -= 1000000000L;
    }
    for (;;) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec > end.tv_sec || (now.tv_sec == end.tv_sec && now.tv_nsec >= end.tv_nsec)) return;
        struct timespec left = {end.tv_sec - now.tv_sec, end.tv_nsec - now.tv_nsec};
        if (left.tv_nsec < 0) {
            left.tv_sec--;
            left.tv_nsec += 1000000000L;
        }
        nanosleep(&left, NULL);
    }
}

static void grandchild(const char *shape, const char *pid_file) {
    int ready[2];
    if (pipe(ready) != 0) die("pipe");
    pid_t child = fork();
    if (child < 0) die("fork");
    if (child == 0) {
        close(ready[0]);
        if (strcmp(shape, "double") == 0 || strcmp(shape, "setsid") == 0 || strcmp(shape, "writer") == 0) {
            pid_t grand = fork();
            if (grand < 0) _exit(126);
            if (grand > 0) {
                close(ready[1]);
                _exit(0);
            }
            /* 孫は、setsid を済ませてから pid を知らせる(品質コマンドが終わる前に群を抜けていることを決める。
             * 先に知らせると、群への TERM と setsid の競り合いになる)。 */
            if (strcmp(shape, "setsid") == 0 || strcmp(shape, "writer") == 0) setsid();
            signal(SIGTERM, SIG_DFL);
            char text[32];
            int len = snprintf(text, sizeof text, "%d\n", (int)getpid());
            ssize_t ignored = write(ready[1], text, (size_t)len);
            (void)ignored;
            close(ready[1]);
            if (strcmp(shape, "writer") == 0) {
                for (int round = 0; round < 3000; round++) {
                    ssize_t ignored = write(1, "w", 1);
                    (void)ignored;
                    usleep(10000);
                }
                _exit(0);
            }
            sleep_for(30);
            _exit(0);
        }
        /* hold: 子自身が stdout を開いたまま眠る(孫は作らない)。 */
        char text[32];
        int len = snprintf(text, sizeof text, "%d\n", (int)getpid());
        ssize_t ignored = write(ready[1], text, (size_t)len);
        (void)ignored;
        close(ready[1]);
        signal(SIGTERM, SIG_DFL);
        sleep_for(30);
        _exit(0);
    }
    close(ready[1]);
    char text[32] = {0};
    ssize_t got = read(ready[0], text, sizeof text - 1);
    close(ready[0]);
    if (got <= 0) die("孫の pid を受け取れない");
    if (strcmp(shape, "hold") != 0) waitpid(child, NULL, 0);
    if (pid_file != NULL) {
        int fd = open(pid_file, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (fd < 0) die("孫の pid を書けない");
        ssize_t ignored = write(fd, text, strlen(text));
        (void)ignored;
        close(fd);
    }
}

static int state_mode(int argc, char **argv) {
    const char *state = argv[2];
    const char *started = NULL;
    const char *shape = NULL;
    const char *pid_file = NULL;
    double sleep_seconds = 0;
    int code = 0;
    int kill_self = 0;
    int ignore_term = 0;
    long out_bytes = 0;
    long err_bytes = 0;
    for (int i = 3; i < argc; i++) {
        const char *option = argv[i];
        const char *value = i + 1 < argc ? argv[i + 1] : NULL;
        if (strcmp(option, "--ignore-term") == 0) {
            ignore_term = 1;
            continue;
        }
        if (value == NULL) {
            fprintf(stderr, "r4q_probe: %s の値が無い\n", option);
            return 125;
        }
        i++;
        if (strcmp(option, "--write-stdout") == 0) out_bytes = atol(value);
        else if (strcmp(option, "--write-stderr") == 0) err_bytes = atol(value);
        else if (strcmp(option, "--term-log") == 0) term_log = value;
        else if (strcmp(option, "--started") == 0) started = value;
        else if (strcmp(option, "--grandchild") == 0) shape = value;
        else if (strcmp(option, "--grandchild-pid") == 0) pid_file = value;
        else if (strcmp(option, "--sleep") == 0) sleep_seconds = atof(value);
        else if (strcmp(option, "--exit") == 0) code = atoi(value);
        else if (strcmp(option, "--kill-self") == 0) kill_self = atoi(value);
        else if (strcmp(option, "--mark") == 0) continue;
        else {
            fprintf(stderr, "r4q_probe: 知らない選択肢 %s\n", option);
            return 125;
        }
    }
    /* 状態は、自分で信号の設定を変える前に読む。 */
    write_state(state);
    if (term_log != NULL || ignore_term) {
        struct sigaction action;
        memset(&action, 0, sizeof action);
        action.sa_handler = on_term;
        sigaction(SIGTERM, &action, NULL);
        sigset_t unblock;
        sigemptyset(&unblock);
        sigaddset(&unblock, SIGTERM);
        sigprocmask(SIG_UNBLOCK, &unblock, NULL);
    }
    if (out_bytes > 0) fill(1, 'o', out_bytes);
    if (err_bytes > 0) fill(2, 'e', err_bytes);
    if (shape != NULL) grandchild(shape, pid_file);
    if (started != NULL) touch(started);
    if (sleep_seconds > 0) {
        if (ignore_term || term_log == NULL) {
            sleep_for(sleep_seconds);
        } else {
            /* TERM を数えながら、最初の TERM で終わる。 */
            struct timespec step = {0, 10000000L};
            double waited = 0;
            while (waited < sleep_seconds && terms == 0) {
                nanosleep(&step, NULL);
                waited += 0.01;
            }
            if (terms > 0) {
                signal(SIGTERM, SIG_DFL);
                raise(SIGTERM);
            }
        }
    }
    if (kill_self) {
        signal(kill_self, SIG_DFL);
        raise(kill_self);
    }
    return code;
}

#ifdef __linux__
static int hold_mode(int argc, char **argv) {
    if (argc < 7 || strcmp(argv[5], "--") != 0) {
        fprintf(stderr, "r4q_probe hold <上限の秒> <比較器の pid> <結果のファイル> -- <コマンド…>\n");
        return 125;
    }
    double limit = atof(argv[2]);
    pid_t expected = (pid_t)atol(argv[3]);
    const char *result = argv[4];
    if (prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0) die("PR_SET_CHILD_SUBREAPER");
    if (prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0) die("PR_SET_PDEATHSIG");
    if (getppid() != expected) {
        /* 設定の前に比較器が死んだ競り合い。自分で回収して終わる。 */
        while (waitpid(-1, NULL, WNOHANG) > 0) {
        }
        return 3;
    }
    pid_t child = fork();
    if (child < 0) die("fork");
    if (child == 0) {
        prctl(PR_SET_PDEATHSIG, 0, 0, 0, 0);
        /* 比べる親の stdin は /dev/null にする(補助の stdin は、比較器が保持を終える合図のパイプ)。 */
        int null = open("/dev/null", O_RDWR);
        if (null < 0 || dup2(null, 0) < 0) _exit(126);
        if (null > 2) close(null);
        execvp(argv[6], argv + 6);
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) die("waitpid");
    }
    int fd = open(result, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd >= 0) {
        char text[32];
        int len = snprintf(text, sizeof text, "%d\n", status);
        ssize_t ignored = write(fd, text, (size_t)len);
        (void)ignored;
        close(fd);
    }
    /* 引き取った子は、stdin が閉じるか上限を過ぎるまで回収しない。 */
    fcntl(0, F_SETFL, fcntl(0, F_GETFL) | O_NONBLOCK);
    double waited = 0;
    while (waited < limit) {
        char byte;
        ssize_t got = read(0, &byte, 1);
        if (got == 0 || (got < 0 && errno != EAGAIN && errno != EINTR)) break;
        usleep(20000);
        waited += 0.02;
    }
    /* 回収は 5 秒まで(生きた子が残っても待ち続けない。残った子は補助の死後に init へ移る)。 */
    for (int round = 0; round < 250; round++) {
        pid_t done = waitpid(-1, NULL, WNOHANG);
        if (done > 0) continue;
        if (done < 0) break;
        usleep(20000);
    }
    return 0;
}
#endif

int main(int argc, char **argv) {
    if (argc >= 3 && strcmp(argv[1], "state") == 0) return state_mode(argc, argv);
#ifdef __linux__
    if (argc >= 2 && strcmp(argv[1], "hold") == 0) return hold_mode(argc, argv);
#endif
    fprintf(stderr, "r4q_probe state <状態のファイル> [選択肢…] | r4q_probe hold …\n");
    return 125;
}
