"""
Генератор PDF из FlowerGraph_Protocols.md
Использует: PyMuPDF (fitz.Story) + python-markdown + pygments
"""
import pathlib
import textwrap
import markdown
from markdown.extensions.tables import TableExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.codehilite import CodeHiliteExtension
import fitz

HERE   = pathlib.Path(__file__).parent
MD_IN  = HERE / 'FlowerGraph_Protocols.md'
PDF_OUT = HERE / 'FlowerGraph_Protocols.pdf'

# ─── CSS ────────────────────────────────────────────────────────────────────

CSS = """
@page {
    size: A4;
    margin: 20mm 18mm 22mm 22mm;
}

body {
    font-family: "DejaVu Sans", Arial, sans-serif;
    font-size: 10pt;
    color: #1a1a1a;
    line-height: 1.55;
}

h1 {
    font-size: 18pt;
    color: #1a3a6a;
    margin-top: 0;
    margin-bottom: 6pt;
    border-bottom: 2pt solid #1a3a6a;
    padding-bottom: 4pt;
}

h2 {
    font-size: 14pt;
    color: #1a3a6a;
    margin-top: 18pt;
    margin-bottom: 4pt;
    border-left: 4pt solid #4a7ac8;
    padding-left: 8pt;
}

h3 {
    font-size: 11pt;
    color: #2a4a8a;
    margin-top: 12pt;
    margin-bottom: 3pt;
}

h4 {
    font-size: 10pt;
    color: #333;
    font-style: italic;
    margin-top: 8pt;
    margin-bottom: 2pt;
}

p {
    margin: 0 0 6pt 0;
}

/* Таблицы */
table {
    border-collapse: collapse;
    width: 100%;
    margin: 8pt 0;
    font-size: 9pt;
}

th {
    background-color: #2a4a8a;
    color: #ffffff;
    padding: 4pt 6pt;
    text-align: left;
    font-weight: bold;
}

td {
    padding: 3pt 6pt;
    border: 0.5pt solid #cccccc;
    vertical-align: top;
}

tr:nth-child(even) td {
    background-color: #f3f6fb;
}

tr:nth-child(odd) td {
    background-color: #ffffff;
}

/* Код */
code {
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 8.5pt;
    background-color: #f0f2f6;
    padding: 1pt 3pt;
    border-radius: 2pt;
    color: #2a2a6a;
}

pre {
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 8pt;
    background-color: #1e2030;
    color: #c8ccd8;
    padding: 8pt 10pt;
    border-left: 3pt solid #4a7ac8;
    margin: 6pt 0;
    line-height: 1.45;
    overflow-x: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
}

pre code {
    background: none;
    padding: 0;
    color: #c8ccd8;
    font-size: 8pt;
}

/* Горизонтальная линия */
hr {
    border: none;
    border-top: 1pt solid #cccccc;
    margin: 14pt 0;
}

/* Списки */
ul, ol {
    margin: 4pt 0;
    padding-left: 18pt;
}

li {
    margin-bottom: 2pt;
}

/* Выделение */
strong {
    color: #1a1a1a;
    font-weight: bold;
}

em {
    color: #444;
}

/* Заголовок страницы (метаданные) */
.doc-meta {
    font-size: 9pt;
    color: #666;
    margin-bottom: 16pt;
    border-bottom: 0.5pt solid #ddd;
    padding-bottom: 6pt;
}

/* Блоки примечаний */
blockquote {
    border-left: 3pt solid #4a7ac8;
    margin: 6pt 0;
    padding: 4pt 10pt;
    background-color: #f0f4fb;
    color: #333;
    font-size: 9.5pt;
}

/* Подсветка синтаксиса (codehilite) */
.codehilite { background: #1e2030; padding: 8pt 10pt; margin: 6pt 0;
              border-left: 3pt solid #4a7ac8; }
.codehilite .hll { background-color: #2a2d4a }
.codehilite .c  { color: #6a8a6a; font-style: italic }   /* Comment */
.codehilite .k  { color: #7ab0e0; font-weight: bold }    /* Keyword */
.codehilite .n  { color: #c8ccd8 }                        /* Name */
.codehilite .s  { color: #a0d0a0 }                        /* String */
.codehilite .m  { color: #e0c070 }                        /* Number */
.codehilite .o  { color: #c0a070 }                        /* Operator */
.codehilite .kd { color: #7ab0e0; font-weight: bold }    /* Keyword.Declaration */
.codehilite .kt { color: #88c0d0; font-weight: bold }    /* Keyword.Type */
.codehilite .cp { color: #a070a0; font-weight: bold }    /* Comment.Preproc */
.codehilite .cm { color: #6a8a6a; font-style: italic }   /* Comment.Multiline */
.codehilite .nf { color: #88c8f0 }                        /* Name.Function */
.codehilite .nc { color: #88d0c0; font-weight: bold }    /* Name.Class */
.codehilite .na { color: #b0c8e8 }                        /* Name.Attribute */
.codehilite .nb { color: #88c0d0 }                        /* Name.Builtin */
"""


# ─── Конвертация MD → HTML ───────────────────────────────────────────────────

def md_to_html(md_path: pathlib.Path) -> str:
    text = md_path.read_text(encoding='utf-8')

    md = markdown.Markdown(extensions=[
        TableExtension(),
        FencedCodeExtension(),
        CodeHiliteExtension(
            linenums=False,
            guess_lang=True,
            css_class='codehilite',
            pygments_style='monokai',
        ),
        'nl2br',
        'toc',
    ])

    body = md.convert(text)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<style>
{CSS}
</style>
</head>
<body>
{body}
</body>
</html>"""
    return html


# ─── Рендеринг HTML → PDF через fitz.Story ───────────────────────────────────

def html_to_pdf(html: str, out_path: pathlib.Path):
    """
    fitz.Story рендерит HTML с CSS в PDF-страницы формата A4.
    """
    # A4 в пунктах (1 мм = 2.8346 pt)
    A4_W  = 595.28
    A4_H  = 841.89
    MG_L  = 62.36   # 22 mm
    MG_R  = 50.91   # 18 mm
    MG_T  = 56.69   # 20 mm
    MG_B  = 62.36   # 22 mm

    story = fitz.Story(html=html, user_css=CSS)

    # Область вёрстки
    mediabox = fitz.Rect(0, 0, A4_W, A4_H)
    where     = fitz.Rect(MG_L, MG_T, A4_W - MG_R, A4_H - MG_B)

    writer  = fitz.DocumentWriter(str(out_path))
    more    = True

    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()

    writer.close()
    print(f'PDF создан: {out_path}  ({out_path.stat().st_size // 1024} КБ)')


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f'Читаю:   {MD_IN}')
    html = md_to_html(MD_IN)
    print(f'HTML:    {len(html):,} символов')
    html_to_pdf(html, PDF_OUT)
