"""Development-only recipes. Acceptance is measured at the legacy helper CLI."""
WRAPPERS = [("[", "]"), ("{a: ", "}"), ('{"a": ', "}"), ("!!seq [", "]"),
            ("!!map {a: ", "}"), ("[0,", "]"), ("[#comment\n", "]"), ("[? a : ", "]")]


def build(recipe):
    kind, *args = recipe
    if kind == "mix":
        a, b, n = args
        pairs = [WRAPPERS[a if k % 2 == 0 else b] for k in range(n)]
        return "".join(left for left, _ in pairs) + ["safe", '"日"', "[]", "{}"][b % 4] + "".join(right for _, right in reversed(pairs))
    if kind == "scalar":
        context, n = args
        defs = [f"&a{i} !!str " + ("safe" if i == 0 else "{=: *a" + str(i - 1) + "}") for i in range(n)]
        if context == "seq":
            return "\n".join("- " + value for value in defs)
        if context == "map":
            return "\n".join(f"a{i}: " + value for i, value in enumerate(defs))
        return "!!pairs\n" + "\n".join(f"- a{i}: " + value for i, value in enumerate(defs))
    if kind == "lookahead":
        spaces, n = args
        return "[" * n + " " * spaces + '"safe"' + "]" * n
    if kind == "block":
        outer, n, leaf = args
        prefix = "".join("  " * i + ("-\n" if outer == "seq" else "a:\n") for i in range(n))
        ending = {"plain": "safe\n", "quoted": '"safe"\n', "literal": "|\n  safe\n", "folded": ">\n  safe\n", "anchor": "&end safe\n"}[leaf]
        return prefix + "  " * n + ending.replace("\n  ", "\n" + "  " * (n + 1))
    family, n = args
    if kind == "value":
        if family == "seq": return "[" * n + "]" * n
        if family == "map": return "{a: " * n + "null" + "}" * n
        if family == "unused_seq": return "unused: " + "[" * n + "]" * n
        if family == "unused_map": return "unused: " + "{a: " * n + "null" + "}" * n
        if family == "alias": return "- &a0 []\n" + "".join(f"- &a{i} [*a{i-1}]\n" for i in range(1, n))
        if family == "merge": return "- &a0 {}\n" + "".join(f"- &a{i} {{<<: *a{i-1}}}\n" for i in range(1, n))
        if family == "scalar": return "!!str " + "{=: !!str " * n + "safe" + "}" * n
        if family == "scalar_alias": return "- &a0 !!str safe\n" + "".join(f"- &a{i} !!str {{=: *a{i-1}}}\n" for i in range(1, n))
    if kind == "data":
        if family.startswith("block_seq"):
            return "".join("  " * i + "-\n" for i in range(n)) + "  " * n + ("safe\n" if family.endswith("scalar") else "[]\n")
        if family == "block_map":
            return "".join("  " * i + "a:\n" for i in range(n)) + "  " * n + "null\n"
        if family.startswith("sequence_"):
            leaf = {"empty": "", "plain": "safe", "double": '"safe"', "single": "'safe'", "tag": "!!str safe", "alias": "*a", "comment": "#comment\n", "null": "null", "number": "12", "long": "a" * 1020}[family[9:]]
            return ("anchor: &a []\nunused: " if family == "sequence_alias" else "") + "[" * n + leaf + "]" * n
        if family.startswith("mapping_"):
            leaf = {"plain": "safe", "double": '"safe"', "single": "'safe'", "tag": "!!str safe", "empty": "{}", "null": "null"}[family[8:]]
            return "{a: " * n + leaf + "}" * n
    raise ValueError(recipe)
