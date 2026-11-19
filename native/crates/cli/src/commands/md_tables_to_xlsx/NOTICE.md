# 第三者の著作物の表示

このディレクトリ(`md_tables_to_xlsx`)の次の部分は、openpyxl 3.1.5 から転記しました。

- `theme1.xml`: 既定テーマ。`openpyxl/writer/theme.py` の `theme_xml` を UTF-8 にしたバイト(10140 バイト)です。
- `xml.rs` の `APP_XML`: `docProps/app.xml` の文。`openpyxl/packaging/extended.py` の `ExtendedProperties` の出力です。
- `xml.rs` の `STYLES_XML`・`ROOT_RELS` と、各部分の XML の要素・属性の並び: openpyxl 3.1.5 が lxml なしの環境で書く出力に合わせました。

出典: openpyxl 3.1.5(<https://foss.heptapod.net/openpyxl/openpyxl>)。
許諾: MIT ライセンス。openpyxl 3.1.5 の配布物の `LICENCE.rst` の全文を、次に写します。

```text
This software is under the MIT Licence
======================================

Copyright (c) 2010 openpyxl

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

openpyxl のソースの各ファイルの先頭には `Copyright (c) 2010-2024 openpyxl` とあります。
