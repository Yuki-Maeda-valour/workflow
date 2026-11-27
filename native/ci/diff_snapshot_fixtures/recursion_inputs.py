"""旧helperのCLIで測った再帰入力。テスト関数のスタック深さへ依存させない。"""


def value(kind,n):
 if kind=="seq": return "["*n+"]"*n
 if kind=="map": return "{a: "*n+"null"+"}"*n
 if kind=="unused_seq": return "unused: "+"["*n+"]"*n
 if kind=="unused_map": return "unused: "+"{a: "*n+"null"+"}"*n
 if kind=="alias": return "- &a0 []\n"+"".join(f"- &a{i} [*a{i-1}]\n" for i in range(1,n))
 if kind=="merge": return "- &a0 {}\n"+"".join(f"- &a{i} {{<<: *a{i-1}}}\n" for i in range(1,n))
 if kind=="scalar": return "!!str "+"{=: !!str "*n+"safe"+"}"*n
 if kind=="scalar_alias": return "- &a0 !!str safe\n"+"".join(f"- &a{i} !!str {{=: *a{i-1}}}\n" for i in range(1,n))
 raise ValueError(kind)

def data(kind,n):
 if kind.startswith('block_seq'): return ''.join('  '*i+'-\n' for i in range(n))+('  '*n+('safe\n' if kind.endswith('scalar') else '[]\n'))
 if kind=='block_map': return ''.join('  '*i+'a:\n' for i in range(n))+'  '*n+'null\n'
 if kind.startswith('sequence_'):
  value={'empty':'','plain':'safe','double':'"safe"','single':"'safe'",'tag':'!!str safe','alias':'*a','comment':'#comment\n','null':'null','number':'12','long':'a'*1020}[kind[9:]]
  prefix='anchor: &a []\nunused: ' if kind=='sequence_alias' else ''
  return prefix+'['*n+value+']'*n
 if kind.startswith('mapping_'):
  value={'plain':'safe','double':'"safe"','single':"'safe'",'tag':'!!str safe','empty':'{}','null':'null'}[kind[8:]]
  return '{a: '*n+value+'}'*n
