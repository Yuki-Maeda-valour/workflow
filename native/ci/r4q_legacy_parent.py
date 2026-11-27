#!/usr/bin/env python3
"""R4Q(#494)の比較器の旧版の親: 旧版の check-process.py を読み込み、要求の JSON で run_command を呼ぶ。

使い方(比較器 check_r4q.py が R4Q_PYTHON -B で起動する):
    r4q_legacy_parent.py <要求の JSON のパス> <Rust の検査用の実行物>

- review-guard の check_process_module と同じく importlib で読み込む(sys.dont_write_bytecode を真にする)。
- run_command は <module> → main → run_command の深さで呼ぶ(Rust の caller_frames の既定 2 と同じ。
  json.loads の入れ子の上限が旧版と同じになる前提)。
- 要求と結果の形は、Rust の --r4q-run-command と同じ(設計の「検査用の要求と結果の JSON の形」)。
  結果は鍵の名前の順・区切りなしの JSON と改行で、Rust の出力とバイト列で比べる。
- 監督: legacy はそのまま(sys.executable・-B・check-process.py)。rust と明示の形は、launch_monitor の
  argv の先頭 3 要素(check-process.py:89・:161)を差し替えてから元の launch_monitor を呼ぶ(ほかは変えない)。
- 比較器の自己検査(test_r4q.py)の偽の Rust は、このファイルを読み込み、旧版のコピーのパスを script に渡して
  main を呼ぶ(呼出しの深さは変わる。入れ子の上限のケースには使わない)。
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/check-process.py"
KEYS = {"argv", "cwd", "env", "timeout", "grace", "method", "keep", "directory", "monitor", "int_max_str_digits"}


def text(value):
    """要求の文字列(UTF-8〈surrogatepass〉の 16 進)を、旧版の str にする。"""
    return bytes.fromhex(value).decode("utf-8", "surrogatepass")


def number(value):
    if set(value) == {"int"}:
        return int(value["int"])
    if set(value) == {"float"}:
        return float(value["float"])
    raise ValueError(f"数の形でない: {value}")


def form(value):
    if value is None:
        return {"none": None}
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": int.__repr__(value)}
    if isinstance(value, float):
        return {"float": float.__repr__(value)}
    if isinstance(value, str):
        return {"str": value.encode("utf-8", "surrogatepass").hex()}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, list):
        return {"list": [form(item) for item in value]}
    if isinstance(value, dict):
        return {"dict": [[form(key), form(item)] for key, item in value.items()]}
    raise TypeError(f"結果に知らない値: {type(value).__name__}")


def load(rust, monitor, script=SCRIPT, override=None):
    spec = importlib.util.spec_from_file_location("check_process", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if override is not None or monitor != "legacy":
        if override is not None:
            # 自己検査の偽の Rust の親だけが使う頭(legacy の本物の旧版・取り違えた頭)。
            head = list(override)
        elif monitor == "rust":
            head = [rust, "--r4q-check-process"]
        else:
            head = [text(monitor["program"]), *(text(item) for item in monitor["prefix"])]
        original = module.launch_monitor

        def launch_monitor(argv, *args, **kwargs):
            return original([*head, *argv[3:]], *args, **kwargs)

        module.launch_monitor = launch_monitor
    return module


def main(argv=None, script=SCRIPT, legacy_head=None, rust_head=None):
    argv = sys.argv[1:] if argv is None else argv
    request = json.loads(Path(argv[0]).read_bytes())
    if set(request) != KEYS:
        raise SystemExit(f"要求の鍵が違う: {sorted(request)}")
    if request["monitor"] not in ("legacy", "rust") and not isinstance(request["monitor"], dict):
        raise SystemExit("monitor は legacy か rust か明示の形")
    if request["int_max_str_digits"] != 4300:
        sys.set_int_max_str_digits(request["int_max_str_digits"])
    # 自己検査の偽の Rust の親だけが使う: legacy の監督を、読み込んだコピーではなく本物の旧版にする(legacy_head)。
    # rust_head は、頭を取り違えた偽の親(比較器が不合格にすることを確かめる)。
    override = {"legacy": legacy_head, "rust": rust_head}.get(request["monitor"]) \
        if isinstance(request["monitor"], str) else None
    module = load(argv[1], request["monitor"], script, override)
    env = None
    if request["env"] is not None:
        env = {}
        for key, value in request["env"]:
            env[text(key)] = text(value)
    cwd = None if request["cwd"] is None else text(request["cwd"])
    stop = module.StopSignals()
    with stop:
        try:
            result = module.run_command([text(item) for item in request["argv"]], cwd, env,
                                        number(request["timeout"]), Path(text(request["directory"])), stop,
                                        grace=number(request["grace"]), method=request["method"],
                                        keep=request["keep"])
            output = {"result": [[form(key), form(value)] for key, value in result.items()]}
        except (module.RecoveryError, Exception) as exc:  # noqa: B014(Interrupted は RecoveryError の子)
            output = {"error": {"type": type(exc).__name__, "message": str(exc).encode("utf-8", "surrogatepass").hex()}}
    sys.stdout.write(json.dumps(output, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
