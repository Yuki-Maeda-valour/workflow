/*
 * R4U(#480)の比較専用の共有ライブラリ。製品には入れない。
 *
 * - write(2) の呼出しを 1 回ずつ記録し、出来事で決めた故障と SIGINT を当てる。
 * - 旧 Python と Rust の検査用の実行物の両方へ、同じ形で差し込む
 *   (Linux は LD_PRELOAD、Mac は DYLD_INSERT_LIBRARIES と __DATA,__interpose)。
 * - 設定は環境変数で渡す。
 *   - R4U_SHIM_LOG: 記録の書き先。`fd:<数>` なら受け継いだ fd(パイプ。RLIMIT_FSIZE の影響を受けない)、
 *     それ以外はファイルのパス(追記)。1 行が 1 回の write の JSON。
 *   - R4U_SHIM_RULES: `;` で区切った規則。規則は `,` で区切った `鍵=値`。
 *     - fd=<数>(-1 は全部)
 *     - 選び方: nth=<その fd の何回目か(1 から)> か、prefix=<16 進>・occ=<何回目か(1 から)>
 *       (exact=1 なら、長さも prefix と同じ write だけを数える)
 *     - action=fail(errno=<数>)・short(bytes=<数>。その数だけ実際に書く)・sigint(実際に書いた後に自分へ SIGINT)
 *       ・quota(bytes=<残りの合計>・errno=<数>。残りを超える分は切り、残りが 0 なら失敗)
 *       ・cut(bytes=<数>・errno=<数>。選んだ write をその数で切り〈0 なら失敗〉、その後の同じ fd の write を
 *         すべて失敗にする。RLIMIT_FSIZE の上限が、選んだ write の途中に来たときと同じ。
 *         選んだ write がその数より短ければ全部を書き、故障は起こさない〈上限に届かない〉)
 *       ・budget(bytes=<上限>・errno=<数>。write ごとに、書いた後の合計が上限以内なら全部を書き、超えるなら
 *         その write だけを失敗にする。後の収まる write は通る。Mac の RLIMIT_FSIZE の動きの模擬)
 *     - map=<16 進の元>:<16 進の置換>|...(budget だけ): 合計の数え方にだけ、元の文字列を置換の長さで数える
 *       (旧版の実行環境のパスを固定名にした長さで判定する。書く中身は変えない)
 *     - persist=1: 選んだ回とその後の同じ fd の write に、同じ動作を当て続ける
 * - 記録の欄: fd・要求の長さ・戻り値・errno・当てた動作・要求したバイト列(16 進)。
 * - 起動の時に、受け継いだ環境変数の名前を 1 行(`{"env":[<16 進>...],"pid":<数>,"exe":"<16 進>",
 *   "values":{<16 進の名前>:<16 進の値>}}`)記録する(比較の前提の外の変数が残っていないことと、ケースが与えた
 *   変数が消えていないことを、比較器が子の中から確かめるため)。値は、比較器が値で扱いを決める 2 つの変数
 *   (`__CF_USER_TEXT_ENCODING`・`__PYVENV_LAUNCHER__`)だけを記録する。
 * - exec の連鎖に耐える(Mac の framework の起動用のプログラムが、本体を posix_spawn の SETEXEC で起動し直す
 *   ときなど)。`fd:` の記録の fd は close-on-exec にせず、連鎖の後のプログラムも同じ fd へ記録する。各プログラムは
 *   起動の時に環境の行を 1 行ずつ書く(pid は同じ)。比較器は最後の行を、実際に動いたプログラムの環境として
 *   確かめる。fd が使えない(EBADF など)ときは abort せず、そのプログラムは記録しない。
 * - 起動の前に閉じていた fd 0〜2 を塞がないよう、ファイルへの記録の fd は 200 以上へ移す。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
#ifdef __linux__
#include <dlfcn.h>
extern char **environ;
#else
#include <crt_externs.h>
#include <mach-o/dyld.h>
#endif

#define MAX_RULES 16
#define MAX_PREFIX 256
#define MAX_MAP 32
#define MAX_FDS 64

enum action { ACT_NONE, ACT_FAIL, ACT_SHORT, ACT_SIGINT, ACT_QUOTA, ACT_CUT, ACT_BUDGET };

struct mapping {
    unsigned char *from;
    size_t from_len;
    size_t to_len;
};

struct rule {
    int fd;
    long nth;
    long occ;
    unsigned char prefix[MAX_PREFIX];
    size_t prefix_len;
    int has_prefix;
    int exact;
    enum action action;
    int err;
    long bytes;
    int persist;
    long seen;      /* prefix に合った回数 */
    int triggered;  /* persist で当て続けている */
    int failing;    /* cut で切った後(その後の write をすべて失敗にする) */
    long used;      /* budget で数えた合計 */
    struct mapping map[MAX_MAP];
    int map_count;
};

static struct rule rules[MAX_RULES];
static int rule_count;
static long calls[MAX_FDS];
static int log_fd = -1;
static int ready;

#ifdef __linux__
typedef ssize_t (*write_fn)(int, const void *, size_t);
static write_fn real_write;
static ssize_t call_real(int fd, const void *buf, size_t count) {
    if (!real_write) {
        real_write = (write_fn)dlsym(RTLD_NEXT, "write");
    }
    return real_write(fd, buf, count);
}
#else
static ssize_t call_real(int fd, const void *buf, size_t count) { return write(fd, buf, count); }
#endif

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void parse_rule(char *text) {
    if (rule_count >= MAX_RULES) {
        abort();
    }
    struct rule *r = &rules[rule_count];
    memset(r, 0, sizeof(*r));
    r->fd = -1;
    char *save = NULL;
    for (char *item = strtok_r(text, ",", &save); item; item = strtok_r(NULL, ",", &save)) {
        char *eq = strchr(item, '=');
        if (!eq) {
            abort();
        }
        *eq = '\0';
        const char *key = item;
        const char *value = eq + 1;
        if (!strcmp(key, "fd")) {
            r->fd = atoi(value);
        } else if (!strcmp(key, "nth")) {
            r->nth = atol(value);
        } else if (!strcmp(key, "occ")) {
            r->occ = atol(value);
        } else if (!strcmp(key, "prefix")) {
            size_t n = strlen(value);
            if (n % 2 || n / 2 > MAX_PREFIX) {
                abort();
            }
            for (size_t i = 0; i < n / 2; i++) {
                int hi = hexval(value[2 * i]), lo = hexval(value[2 * i + 1]);
                if (hi < 0 || lo < 0) {
                    abort();
                }
                r->prefix[i] = (unsigned char)(hi * 16 + lo);
            }
            r->prefix_len = n / 2;
            r->has_prefix = 1;
        } else if (!strcmp(key, "action")) {
            if (!strcmp(value, "fail")) r->action = ACT_FAIL;
            else if (!strcmp(value, "short")) r->action = ACT_SHORT;
            else if (!strcmp(value, "sigint")) r->action = ACT_SIGINT;
            else if (!strcmp(value, "quota")) r->action = ACT_QUOTA;
            else if (!strcmp(value, "cut")) r->action = ACT_CUT;
            else if (!strcmp(value, "budget")) r->action = ACT_BUDGET;
            else abort();
        } else if (!strcmp(key, "errno")) {
            r->err = atoi(value);
        } else if (!strcmp(key, "bytes")) {
            r->bytes = atol(value);
        } else if (!strcmp(key, "exact")) {
            r->exact = atoi(value);
        } else if (!strcmp(key, "map")) {
            char *pairs = strdup(value);
            if (!pairs) {
                abort();
            }
            char *keep = NULL;
            for (char *pair = strtok_r(pairs, "|", &keep); pair; pair = strtok_r(NULL, "|", &keep)) {
                char *colon = strchr(pair, ':');
                if (!colon || r->map_count >= MAX_MAP) {
                    abort();
                }
                *colon = '\0';
                size_t n = strlen(pair), m = strlen(colon + 1);
                if (n % 2 || m % 2 || n == 0) {
                    abort();
                }
                struct mapping *mp = &r->map[r->map_count++];
                mp->from = malloc(n / 2);
                if (!mp->from) {
                    abort();
                }
                for (size_t i = 0; i < n / 2; i++) {
                    int hi = hexval(pair[2 * i]), lo = hexval(pair[2 * i + 1]);
                    if (hi < 0 || lo < 0) {
                        abort();
                    }
                    mp->from[i] = (unsigned char)(hi * 16 + lo);
                }
                mp->from_len = n / 2;
                mp->to_len = m / 2;
            }
            free(pairs);
        } else if (!strcmp(key, "persist")) {
            r->persist = atoi(value);
        } else {
            abort();
        }
    }
    if (r->action == ACT_NONE ||
        (r->nth <= 0 && !r->has_prefix && r->action != ACT_QUOTA && r->action != ACT_BUDGET)) {
        abort();
    }
    if (r->has_prefix && r->occ <= 0) {
        r->occ = 1;
    }
    rule_count++;
}

static void record_env(void);

__attribute__((constructor)) static void shim_init(void) {
    const char *path = getenv("R4U_SHIM_LOG");
    if (path && !strncmp(path, "fd:", 3)) {
        log_fd = atoi(path + 3);
        if (log_fd < 3) {
            abort();
        }
        if (fcntl(log_fd, F_GETFD) < 0) {
            /* exec の連鎖の前のプログラムが閉じたなど。abort せず、このプログラムは記録しない。 */
            log_fd = -1;
        }
    } else if (path && *path) {
        int fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
        if (fd < 0) {
            abort();
        }
        int moved = fcntl(fd, F_DUPFD_CLOEXEC, 200);
        if (moved < 0) {
            abort();
        }
        close(fd);
        log_fd = moved;
    }
    const char *spec = getenv("R4U_SHIM_RULES");
    if (spec && *spec) {
        char *copy = strdup(spec);
        if (!copy) {
            abort();
        }
        char *save = NULL;
        for (char *item = strtok_r(copy, ";", &save); item; item = strtok_r(NULL, ";", &save)) {
            parse_rule(item);
        }
        free(copy);
    }
    record_env();
    ready = 1;
}

static void put(const char *text, size_t len) {
    while (len > 0) {
        ssize_t n = call_real(log_fd, text, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            abort();
        }
        text += n;
        len -= (size_t)n;
    }
}

static void put_num(long long value) {
    char buf[32];
    int i = 31;
    int negative = value < 0;
    unsigned long long v = negative ? (unsigned long long)(-(value + 1)) + 1 : (unsigned long long)value;
    buf[i] = '\0';
    do {
        buf[--i] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    if (negative) buf[--i] = '-';
    put(buf + i, (size_t)(31 - i));
}

static void record(int fd, const void *buf, size_t count, ssize_t ret, int err, const char *action) {
    if (log_fd < 0) {
        return;
    }
    static const char digits[] = "0123456789abcdef";
    put("{\"fd\":", 6);
    put_num(fd);
    put(",\"len\":", 7);
    put_num((long long)count);
    put(",\"ret\":", 7);
    put_num((long long)ret);
    put(",\"errno\":", 9);
    put_num(ret < 0 ? err : 0);
    put(",\"action\":\"", 11);
    put(action, strlen(action));
    put("\",\"data\":\"", 10);
    const unsigned char *bytes = buf;
    char chunk[512];
    size_t used = 0;
    for (size_t i = 0; i < count; i++) {
        chunk[used++] = digits[bytes[i] >> 4];
        chunk[used++] = digits[bytes[i] & 15];
        if (used == sizeof(chunk)) {
            put(chunk, used);
            used = 0;
        }
    }
    put(chunk, used);
    put("\"}\n", 3);
}

static void put_str(const char *text) { put(text, strlen(text)); }

static void record_env(void) {
    if (log_fd < 0) {
        return;
    }
    static const char digits[] = "0123456789abcdef";
#ifdef __linux__
    char **env = environ;
#else
    char **env = *_NSGetEnviron();
#endif
    put_str("{\"env\":[");
    for (int i = 0; env && env[i]; i++) {
        const char *eq = strchr(env[i], '=');
        size_t n = eq ? (size_t)(eq - env[i]) : strlen(env[i]);
        if (i) put_str(",");
        put_str("\"");
        for (size_t j = 0; j < n; j++) {
            char pair[2] = {digits[(unsigned char)env[i][j] >> 4], digits[(unsigned char)env[i][j] & 15]};
            put(pair, 2);
        }
        put_str("\"");
    }
    put_str("],\"values\":{");
    int shown = 0;
    for (int i = 0; env && env[i]; i++) {
        static const char *const names[] = {"__CF_USER_TEXT_ENCODING=", "__PYVENV_LAUNCHER__="};
        for (size_t k = 0; k < sizeof(names) / sizeof(names[0]); k++) {
            size_t n = strlen(names[k]);
            if (strncmp(env[i], names[k], n) != 0) continue;
            if (shown++) put_str(",");
            put_str("\"");
            for (size_t j = 0; j + 1 < n; j++) {
                char pair[2] = {digits[(unsigned char)names[k][j] >> 4], digits[(unsigned char)names[k][j] & 15]};
                put(pair, 2);
            }
            put_str("\":\"");
            for (const char *v = env[i] + n; *v; v++) {
                char pair[2] = {digits[(unsigned char)*v >> 4], digits[(unsigned char)*v & 15]};
                put(pair, 2);
            }
            put_str("\"");
        }
    }
    put_str("},\"pid\":");
    put_num((long long)getpid());
    char exe[4096];
    size_t exe_len = 0;
#ifdef __linux__
    ssize_t got = readlink("/proc/self/exe", exe, sizeof(exe));
    if (got > 0) exe_len = (size_t)got;
#else
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) == 0) exe_len = strnlen(exe, sizeof(exe));
#endif
    put_str(",\"exe\":\"");
    for (size_t j = 0; j < exe_len; j++) {
        char pair[2] = {digits[(unsigned char)exe[j] >> 4], digits[(unsigned char)exe[j] & 15]};
        put(pair, 2);
    }
    put_str("\"}\n");
}

/* budget の合計に数える長さ: map の元の文字列を、置換の長さで数える(重ならずに左から)。 */
static long effective_length(const struct rule *r, const void *buf, size_t count) {
    long length = (long)count;
    const unsigned char *bytes = buf;
    for (int m = 0; m < r->map_count; m++) {
        const struct mapping *mp = &r->map[m];
        size_t at = 0;
        while (at + mp->from_len <= count) {
            const unsigned char *hit = memmem(bytes + at, count - at, mp->from, mp->from_len);
            if (!hit) {
                break;
            }
            length += (long)mp->to_len - (long)mp->from_len;
            at = (size_t)(hit - bytes) + mp->from_len;
        }
    }
    return length;
}

static struct rule *select_rule(int fd, const void *buf, size_t count, long nth) {
    struct rule *chosen = NULL;
    for (int i = 0; i < rule_count; i++) {
        struct rule *r = &rules[i];
        if (r->fd != -1 && r->fd != fd) continue;
        int hit = 0;
        if (r->failing) {
            hit = 1;
        } else if (r->triggered) {
            hit = 1;
        } else if ((r->action == ACT_QUOTA || r->action == ACT_BUDGET) && r->nth <= 0 && !r->has_prefix) {
            hit = 1;
        } else if (r->has_prefix) {
            if (count >= r->prefix_len && (!r->exact || count == r->prefix_len) &&
                memcmp(buf, r->prefix, r->prefix_len) == 0) {
                r->seen++;
                hit = r->seen == r->occ;
            }
        } else if (r->nth == nth) {
            hit = 1;
        }
        if (hit) {
            if (r->persist) r->triggered = 1;
            if (!chosen) chosen = r;
        }
    }
    return chosen;
}

ssize_t r4u_write(int fd, const void *buf, size_t count) {
    if (!ready || log_fd < 0 || fd == log_fd) {
        return call_real(fd, buf, count);
    }
    int saved = errno;
    long nth = 0;
    if (fd >= 0 && fd < MAX_FDS) {
        nth = ++calls[fd];
    }
    struct rule *r = select_rule(fd, buf, count, nth);
    ssize_t ret;
    int err = 0;
    const char *action = "none";
    errno = saved;
    if (r && r->action == ACT_FAIL) {
        ret = -1;
        err = r->err;
        action = "fail";
    } else if (r && r->action == ACT_SHORT) {
        size_t n = (size_t)r->bytes < count ? (size_t)r->bytes : count;
        ret = call_real(fd, buf, n);
        err = errno;
        action = "short";
    } else if (r && r->action == ACT_CUT) {
        if (r->failing) {
            ret = -1;
            err = r->err;
            action = "cut-fail";
        } else if ((size_t)r->bytes >= count) {
            ret = call_real(fd, buf, count);
            err = errno;
            action = "cut-full";
        } else if (r->bytes <= 0) {
            ret = -1;
            err = r->err;
            r->failing = 1;
            action = "cut";
        } else {
            ret = call_real(fd, buf, (size_t)r->bytes);
            err = errno;
            r->failing = 1;
            action = "cut";
        }
    } else if (r && r->action == ACT_BUDGET) {
        long need = effective_length(r, buf, count);
        if (need <= r->bytes - r->used) {
            ret = call_real(fd, buf, count);
            err = errno;
            if (ret == (ssize_t)count) {
                r->used += need;
            } else if (ret > 0) {
                r->used += ret;
            }
            action = "budget";
        } else {
            ret = -1;
            err = r->err;
            action = "budget-fail";
        }
    } else if (r && r->action == ACT_QUOTA) {
        if (r->bytes <= 0) {
            ret = -1;
            err = r->err;
            action = "quota-fail";
        } else {
            size_t n = (size_t)r->bytes < count ? (size_t)r->bytes : count;
            ret = call_real(fd, buf, n);
            err = errno;
            if (ret > 0) r->bytes -= ret;
            action = n < count ? "quota-short" : "quota";
        }
    } else {
        ret = call_real(fd, buf, count);
        err = errno;
        if (r && r->action == ACT_SIGINT) action = "sigint";
    }
    record(fd, buf, count, ret, err, action);
    if (r && r->action == ACT_SIGINT) {
        kill(getpid(), SIGINT);
    }
    errno = ret < 0 ? err : saved;
    return ret;
}

#ifdef __APPLE__
struct interpose {
    const void *replacement;
    const void *original;
};
__attribute__((used, section("__DATA,__interpose"))) static const struct interpose interpose_write[] = {
    {(const void *)(uintptr_t)&r4u_write, (const void *)(uintptr_t)&write},
};
#else
ssize_t write(int fd, const void *buf, size_t count) { return r4u_write(fd, buf, count); }
#endif
