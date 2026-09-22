#!/usr/bin/env python3
"""scripts/validate.py の回帰テスト。

各検査について「**わざと壊した写しで ERROR になる**」「**正しい写しでは ERROR にならない**」の
2 方向を見る(片方だけだと、検査を外しても・広げすぎても緑のままになってしまう)。
写しは setUp が毎回作り直すので、テスト同士は干渉しない。
"""

import contextlib
import io
import json
import os
import re
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_REPO = Path(__file__).resolve().parent.parent

# 周辺プロジェクト名を渡す環境変数(validate.py の _PROJECT_NAMES_ENV と対)
PROJECT_NAMES_ENV = "WORKFLOW_PROJECT_NAMES"

# 書き込み先。禁止パターン・委託の語の検査対象は scripts/ 配下の非画像ファイル、
# リンク検査の対象は skill 直下と references/ 配下の *.md —— 検査ごとに対象が違うので
# 書き先を選べるようにする。
CHECKED_PY = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
CHECKED_MD = "plugins/dev-workflow/skills/tool-check/references/validate-test.md"
SKILLS_REL = "plugins/dev-workflow/skills"
SKILL_COUNT_RE = re.compile(r"skills?\s*(\d+)\s*種|(\d+)\s*skills?")
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"
PLUGIN_JSON = "plugins/dev-workflow/.claude-plugin/plugin.json"


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "workflow"
        shutil.copytree(
            SOURCE_REPO,
            self.repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        self.home = self.root / "home"
        # 偽 home に「いかにも拾いそうな」周辺プロジェクトを置く。V3 の核心は
        # **これが在っても環境変数を渡さない限り検査しない**こと —— この 1 行が無いと、
        # `~/dev` の列挙を復活させる実装でもテストが全部通ってしまう。
        (self.home / "dev" / "customer-portal").mkdir(parents=True)

    # ------------------------------------------------------------------ ヘルパ

    def run_validator(self, env=None):
        """写しの validate.py を走らせる。`env` で渡した環境変数だけが見える状態にする
        (実行環境の WORKFLOW_PROJECT_NAMES を持ち込まない)。"""
        script = self.repo / "scripts" / "validate.py"
        environ = {k: v for k, v in os.environ.items() if k != PROJECT_NAMES_ENV}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch(
            "pathlib.Path.home", return_value=self.home
        ):
            namespace = runpy.run_path(str(script), run_name="validate_under_test")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = namespace["main"]()
        return status, namespace["ERRORS"], output.getvalue()

    def append_to_checked_file(self, text, relpath=CHECKED_PY, prefix="# "):
        target = self.repo / relpath
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{prefix}{text}\n")

    def write_file(self, relpath, text):
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_skill_md(self, skill, total_lines, trailing_newline=True):
        """SKILL.md を frontmatter を残したまま**論理行数ちょうど**に作り替える。"""
        target = self.repo / "plugins" / "dev-workflow" / "skills" / skill / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        end = text.index("\n---", 3) + len("\n---\n")
        front = text[:end]
        filler_count = total_lines - front.count("\n")
        self.assertGreater(filler_count, 0, "frontmatter だけで指定行数を超えている")
        out = front + "\n".join(["本文"] * filler_count)
        if trailing_newline:
            out += "\n"
        target.write_text(out, encoding="utf-8")
        # validate.py と同じ数え方で、狙った論理行数になったことを確かめてから使う
        logical = out.count("\n") + (0 if out.endswith("\n") else 1)
        self.assertEqual(total_lines, logical)

    def set_description(self, skill, length):
        target = self.repo / "plugins" / "dev-workflow" / "skills" / skill / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        new, n = re.subn(
            r"^description: .*$", "description: " + "あ" * length, text, count=1, flags=re.MULTILINE
        )
        self.assertEqual(1, n, "description 行が 1 行見つからない")
        target.write_text(new, encoding="utf-8")

    def patch_json(self, relpath, mutate):
        target = self.repo / relpath
        data = json.loads(target.read_text(encoding="utf-8"))
        mutate(data)
        target.write_text(json.dumps(data, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")

    def assert_clean(self, env=None):
        status, errors, _ = self.run_validator(env)
        self.assertEqual(0, status, errors)
        self.assertEqual([], errors)

    def assert_error(self, needles, env=None):
        """ERROR で落ち、**その本文に該当の理由が出る**ことまで見る。"""
        status, errors, _ = self.run_validator(env)
        self.assertEqual(1, status, errors)
        self.assertTrue(
            any(all(needle in error for needle in needles) for error in errors), errors
        )
        return errors

    # -------------------------------------------------------------- 写しの健全性

    def test_pristine_copy_is_clean(self):
        """壊していない写しは ERROR 0(各テストの「ERROR にならない」判定の土台)。"""
        self.assert_clean()

    # ----------------------------------------------- V1: 禁止パターンの免除マーカー

    def test_v1_exemption_word_alone_does_not_exempt(self):
        # 旧実装は「しない」を含む行を一律に免除していた
        self.append_to_checked_file(f"{self.home / 'private'} は使用しない")
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_marker_with_reason_exempts_the_line(self):
        self.append_to_checked_file(
            f"{self.home / 'private'} <!-- validate-allow: 規約の説明のための例示 -->"
        )
        self.assert_clean()

    def test_v1_marker_without_reason_does_not_exempt(self):
        self.append_to_checked_file(f"{self.home / 'private'} <!-- validate-allow -->")
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_empty_reason_followed_by_another_comment_does_not_exempt(self):
        # 理由を「最初のコメント終端まで」で取らないと、後ろのコメントの `-->` まで
        # 飲み込んで「理由あり」に化ける
        self.append_to_checked_file(
            f"{self.home / 'private'} <!-- validate-allow: --> <!-- 別のコメント -->"
        )
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_marker_does_not_exempt_delegation_words(self):
        # マーカーが救うのは禁止パターン検査だけ。委託の語は免除規則を持たない
        self.append_to_checked_file(
            "claude-opus-5 は使わない <!-- validate-allow: モデル ID の例示 -->"
        )
        status, errors, _ = self.run_validator()
        self.assertEqual(1, status, errors)
        self.assertTrue(any("委託の語" in e and "'opus'" in e for e in errors), errors)
        self.assertFalse(any("日付付き" in e for e in errors), errors)

    def test_v1_code_fence_does_not_exempt(self):
        # フェンス除外はリンク検査だけに掛ける(禁止パターンは行単位のマーカーだけ)
        self.write_file(CHECKED_MD, f"# t\n\n```\n{self.home / 'private'}\n```\n")
        self.assert_error(["ユーザー環境の絶対パス"])

    # ------------------------------------------------------ V2: SKILL.md の行数

    def test_v2_500_lines_with_trailing_newline_passes(self):
        self.write_skill_md("tool-check", 500, trailing_newline=True)
        self.assert_clean()

    def test_v2_501_lines_with_trailing_newline_is_error(self):
        self.write_skill_md("tool-check", 501, trailing_newline=True)
        self.assert_error(["tool-check/SKILL.md", "501 行"])

    def test_v2_500_lines_without_trailing_newline_passes(self):
        self.write_skill_md("tool-check", 500, trailing_newline=False)
        self.assert_clean()

    def test_v2_501_lines_without_trailing_newline_is_error(self):
        # `wc -l` はこれを 500 と数える —— そちらに揃えると規約違反を見逃す
        self.write_skill_md("tool-check", 501, trailing_newline=False)
        self.assert_error(["tool-check/SKILL.md", "501 行"])

    # -------------------------------------------- V3: 周辺プロジェクト名(環境変数)

    def test_v3_without_env_no_project_name_is_checked(self):
        # 偽 home に dev/customer-portal が在っても、環境変数が無ければ検査しない
        # (実行環境のファイルシステムを読まないことの判別機)
        self.assertTrue((self.home / "dev" / "customer-portal").is_dir())
        self.append_to_checked_file("customer-portal に配線する")
        self.assert_clean()

    def test_v3_with_env_project_name_is_detected(self):
        self.append_to_checked_file("customer-portal に配線する")
        self.assert_error(
            ["周辺プロジェクト固有名の混入", "customer-portal"],
            env={PROJECT_NAMES_ENV: "customer-portal"},
        )

    def test_v3_generic_name_is_excluded_even_when_passed(self):
        self.append_to_checked_file("base に配線する")
        self.assert_clean(env={PROJECT_NAMES_ENV: "base"})

    def test_v3_japanese_adjacent_is_detected(self):
        # `\b` は日本語を単語文字として扱うので、旧実装はこの 2 形を取りこぼしていた
        self.append_to_checked_file("customer-portalに配線する")
        self.append_to_checked_file("のcustomer-portalを見る")
        errors = self.assert_error(
            ["周辺プロジェクト固有名の混入"], env={PROJECT_NAMES_ENV: "customer-portal"}
        )
        self.assertEqual(2, sum("周辺プロジェクト固有名の混入" in e for e in errors), errors)

    def test_v3_alnum_and_underscore_adjacent_is_not_detected(self):
        # 除外集合を `[A-Za-z]` だけにすると、この 3 形で誤検出が出る
        for text in ("customer-portal123", "_customer-portal", "customer-portals"):
            self.append_to_checked_file(text)
        self.assert_clean(env={PROJECT_NAMES_ENV: "customer-portal"})

    def test_v3_hyphen_terminated_name_keeps_old_hits(self):
        # 境界を固定で両側に付ける実装はここで取りこぼす(`\b` からの退行)
        self.append_to_checked_file("Proj-x")
        self.append_to_checked_file("Proj-2")
        errors = self.assert_error(
            ["周辺プロジェクト固有名の混入", "Proj-"], env={PROJECT_NAMES_ENV: "Proj-"}
        )
        self.assertEqual(2, sum("周辺プロジェクト固有名の混入" in e for e in errors), errors)

    # ---------------------------------------------------- V4: description の字数

    def test_v4_too_short_description_is_error(self):
        self.set_description("tool-check", 149)
        self.assert_error(["tool-check/SKILL.md", "description 149 字"])

    def test_v4_too_long_description_is_error(self):
        self.set_description("tool-check", 501)
        self.assert_error(["tool-check/SKILL.md", "description 501 字"])

    def test_v4_boundary_descriptions_pass(self):
        for length in (150, 500):
            with self.subTest(length=length):
                self.set_description("tool-check", length)
                self.assert_clean()

    # ------------------------------------------------------------ V5: リンク検査

    def test_v5_broken_link_with_fragment_is_error(self):
        self.write_file(CHECKED_MD, "# t\n\n[x](nope.md#sec)\n")
        self.assert_error(["リンク切れ", "nope.md#sec"])

    def test_v5_broken_link_with_title_is_error(self):
        self.write_file(CHECKED_MD, '# t\n\n[x](nope.md "t")\n')
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_broken_link_inside_code_fence_is_ignored(self):
        for opening, closing in (("```", "```"), ("~~~", "~~~")):
            with self.subTest(fence=opening):
                self.write_file(
                    CHECKED_MD, f"# t\n\n{opening}\n[x](nope.md)\n{closing}\n"
                )
                self.assert_clean()

    def test_v5_fence_is_not_closed_by_a_different_character(self):
        # ~~~ で開いたブロックは ``` では閉じない(閉じたと誤認すると、以降の内外が反転する)
        self.write_file(CHECKED_MD, "# t\n\n~~~\n```\n[x](nope.md)\n~~~\n")
        self.assert_clean()

    def test_v5_longer_fence_is_not_closed_by_a_shorter_one(self):
        # ```` の中の ``` は閉じフェンスにならない(CommonMark)
        self.write_file(CHECKED_MD, "# t\n\n````\n```\n[x](nope.md)\n```\n````\n")
        self.assert_clean()

    def test_v5_closing_fence_must_not_have_an_info_string(self):
        # info string を持つ行は閉じフェンスにならない(CommonMark)。これを見落とすと
        # そこで閉じたことになり、以降の内外が反転して検査が黙って止まる
        self.write_file(CHECKED_MD, "# t\n\n~~~\n~~~text\n[x](nope.md)\n~~~\n")
        self.assert_clean()

    def test_v5_indented_four_spaces_does_not_open_a_fence(self):
        # 4 空白以上の字下げはインデントコードブロックで、フェンスを開かない(CommonMark)。
        # ここで開いてしまうと、以降の実リンクが検査されなくなる
        self.write_file(CHECKED_MD, "# t\n\n    ```\n\n[x](nope.md)\n")
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_inline_code_at_line_start_is_not_a_fence(self):
        # 行頭のインラインコードをフェンス開始と誤認すると、そこから下が丸ごと
        # 検査されなくなる(取りこぼし側の退行)
        self.write_file(CHECKED_MD, "# t\n\n```x``` を使う\n[y](nope.md)\n")
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_existing_link_with_fragment_passes(self):
        self.write_file(CHECKED_MD, "# t\n\n[x](ok.md#sec)\n")
        self.write_file(str(Path(CHECKED_MD).parent / "ok.md"), "# ok\n")
        self.assert_clean()

    def test_v5_existing_link_with_title_passes(self):
        # タイトルをパスに含めて「実在しない」と誤判定しないこと
        self.write_file(CHECKED_MD, '# t\n\n[x](ok.md "t")\n')
        self.write_file(str(Path(CHECKED_MD).parent / "ok.md"), "# ok\n")
        self.assert_clean()

    # -------------------------------------------------------------- V6: 配布メタ

    def test_v6_plugin_version_mismatch_is_error(self):
        self.patch_json(PLUGIN_JSON, lambda d: d.__setitem__("version", "9.9.9"))
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_marketplace_metadata_version_mismatch_is_error(self):
        self.patch_json(MARKETPLACE_JSON, lambda d: d["metadata"].__setitem__("version", "9.9.9"))
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_missing_source_is_error(self):
        self.patch_json(
            MARKETPLACE_JSON, lambda d: d["plugins"][0].__setitem__("source", "./nope")
        )
        self.assert_error(["source が実在しない", "./nope"])

    def test_v6_skill_count_claim_mismatch_is_error(self):
        self.patch_json(
            MARKETPLACE_JSON,
            lambda d: d["plugins"][0].__setitem__(
                "description", d["plugins"][0]["description"].replace("skills 12 種", "skills 99 種")
            ),
        )
        self.assert_error(["skill 件数の表記 99 件", "実数 12 件"])

    def test_v6_plugin_json_skill_count_mismatch_is_error(self):
        # marketplace.json 側だけを壊すと、plugin.json 側の検査を消しても気づけない
        self.patch_json(
            PLUGIN_JSON,
            lambda d: d.__setitem__("description", d["description"].replace("12 skills", "99 skills")),
        )
        self.assert_error(["plugin.json", "skill 件数の表記 99 件", "実数 12 件"])

    def test_v6_all_versions_missing_is_error(self):
        # 3 箇所とも欠けていると set の要素数が 1 になり、空虚に「一致」して通ってしまう
        self.patch_json(PLUGIN_JSON, lambda d: d.pop("version", None))
        self.patch_json(
            MARKETPLACE_JSON,
            lambda d: (d["metadata"].pop("version", None), d["plugins"][0].pop("version", None)),
        )
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_intact_distribution_meta_passes(self):
        # 「何も壊さない写し」。版 3 箇所・source・件数の表記が実際に揃っていることを
        # 直接確かめてから、検査が通ることを見る(無改変の写しを見るだけでは、
        # この検査が動いていなくても緑になる)
        mp = json.loads((self.repo / MARKETPLACE_JSON).read_text(encoding="utf-8"))
        pj = json.loads((self.repo / PLUGIN_JSON).read_text(encoding="utf-8"))
        versions = {mp["metadata"]["version"], pj["version"]} | {
            pl["version"] for pl in mp["plugins"]
        }
        self.assertEqual(1, len(versions), versions)
        self.assertTrue((self.repo / mp["plugins"][0]["source"]).exists())
        actual = len([d for d in (self.repo / SKILLS_REL).iterdir() if d.is_dir()])
        for desc in (mp["plugins"][0]["description"], pj["description"]):
            found = {int(m.group(1) or m.group(2)) for m in SKILL_COUNT_RE.finditer(desc)}
            self.assertEqual({actual}, found, desc[:60])
        self.assert_clean()

    # ------------------------------------------------------------- V8: 委託の語

    def test_v8_japanese_after_agent_is_detected(self):
        self.append_to_checked_file("Agentに委託する")
        self.assert_error(["委託の語", "'Agent'"])

    def test_v8_japanese_before_agent_is_detected(self):
        # 後方だけの否定先読みでは取りこぼす形
        self.append_to_checked_file("委託はAgentで")
        self.assert_error(["委託の語", "'Agent'"])

    def test_v8_alpha_adjacent_words_are_not_detected(self):
        # 既存の検査語(ListAgents)を含まないダミー語を選ぶ。
        # `SubAgent` は**前に英字が続く**形で、否定後読みが無いと誤検出になる
        for text in ("Agentworks", "SubAgent"):
            self.append_to_checked_file(text)
        status, errors, _ = self.run_validator()
        self.assertEqual(0, sum("委託の語" in e for e in errors), errors)
        self.assertEqual(0, status, errors)

    # ------------------------------------------------------ ホスト CLI 語の検査

    def test_host_cli_words_are_detected(self):
        # 書き先は references/*.md に固定する —— CHECKED_PY は scripts/ 配下で
        # _in_host_cli_scope() の対象外なので、そこに書くと ERROR 0 になり空虚に真になる
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(CHECKED_MD, f"# t\n\n{word} を使う\n")
                self.assert_error(["ホスト固有の CLI 語", word])

    def test_host_cli_words_are_not_detected_in_scripts_dir(self):
        # 上のテストの「書き先を固定する」理由を固定する(scripts/ は対象外)
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            self.append_to_checked_file(word)
        self.assert_clean()

    def test_host_cli_words_are_exempted_by_marker(self):
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(
                    CHECKED_MD,
                    f"# t\n\n{word} を使う <!-- validate-allow: 生成する設定の識別子 -->\n",
                )
                self.assert_clean()

    def test_host_cli_marker_exempts_only_its_own_line(self):
        # 免除は**その行だけ**に効く。ファイル単位にすると、一度マーカーを置いた
        # ファイルでは以後どれだけ混入しても捕まらなくなる(設計がファイル全体の
        # 除外を退けた理由そのもの)
        self.write_file(
            CHECKED_MD,
            "# t\n\nchrome-devtools <!-- validate-allow: 生成する設定の識別子 -->\n"
            "chrome-devtools を素で足す\n",
        )
        errors = self.assert_error(["ホスト固有の CLI 語", "chrome-devtools"])
        # **1 件だけ**であることまで見る。件数を見ないと、配線を丸ごと外して
        # 2 件になった場合(免除が一切効かない退行)を素通りさせる
        hits = [e for e in errors if "ホスト固有の CLI 語" in e]
        self.assertEqual(1, len(hits), hits)

    def test_host_cli_marker_without_reason_does_not_exempt(self):
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(CHECKED_MD, f"# t\n\n{word} を使う <!-- validate-allow -->\n")
                self.assert_error(["ホスト固有の CLI 語", word])

    def test_marker_does_not_exempt_delegation_words(self):
        # マーカーが効くのは禁止パターン検査とホスト CLI 語検査の 2 つだけ。
        # 委託の語は役割語へ書き換えて消すので免除規則を持たない
        self.write_file(
            CHECKED_MD, "# t\n\nAgentに委託する <!-- validate-allow: 理由 -->\n"
        )
        self.assert_error(["委託の語", "'Agent'"])

    def test_marker_does_not_exempt_delegation_map_invariant(self):
        target = self.repo / "plugins" / "dev-workflow" / "skills" / "do-task" / "references" / "delegation-map.md"
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\nopus を使う <!-- validate-allow: 理由 -->\n")
        self.assert_error(["除外の不変条件", "'opus'"])

    # ---------------------------------------------- 既存テスト(V3 の方式に追従)

    def test_base_directory_is_treated_as_generic(self):
        self.append_to_checked_file("base")
        self.assert_clean(env={PROJECT_NAMES_ENV: "base"})

    def test_project_specific_directory_name_is_detected(self):
        project_name = "customer-portal"
        self.append_to_checked_file(project_name)
        self.assert_error(
            ["周辺プロジェクト固有名の混入", project_name], env={PROJECT_NAMES_ENV: project_name}
        )

    def test_user_absolute_path_is_detected(self):
        absolute_path = self.home / "private" / "notes"
        self.append_to_checked_file(str(absolute_path))
        self.assert_error(["ユーザー環境の絶対パス", str(self.home)])


if __name__ == "__main__":
    unittest.main()
