"""Генератор PDF-заглушек юридических документов.

Заглушки нужны, чтобы ссылки в интерфейсе (футер, cookie-баннер) вели на
реальные скачиваемые файлы ещё до того, как тексты документов вычитаны.
Файлы кладутся в docs/legal/ и отдаются бэкендом как download-роуты
/terms, /privacy, /cookies, /recommendations.

Запуск (нужен reportlab, в runtime-зависимости проекта НЕ входит):

    python backend/build_legal_stubs.py

После публикации настоящих редакций этот скрипт и файлы *-draft.pdf удаляются.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "legal"

INK = colors.HexColor("#0b1a33")
MUTED = colors.HexColor("#69758a")
LINE = colors.HexColor("#d7dee8")
ACCENT = colors.HexColor("#0099ff")
DANGER_BG = colors.HexColor("#fff1f0")

OPERATOR = (
    "Оператор: Осмольский Егор Александрович, ИНН 422193686126, "
    "самозанятый (НПД), г. Новокузнецк · chetamzavtra@ya.ru"
)

# slug → (заголовок, назначение документа, что в нём будет)
DOCS: list[tuple[str, str, str, list[str]]] = [
    (
        "terms",
        "Пользовательское соглашение",
        "Условия использования сайта чотамзавтра.рф и размещения событий.",
        [
            "правила доступа к сайту и регистрации (в том числе через внешние сервисы);",
            "права и обязанности организаторов при размещении событий;",
            "порядок модерации, отклонения и снятия событий;",
            "ответственность сторон и ограничение ответственности платформы "
            "как информационного посредника (ст. 1253.1 ГК РФ);",
            "порядок изменения соглашения и разрешения споров.",
        ],
    ),
    (
        "privacy",
        "Политика конфиденциальности",
        "Политика обработки персональных данных в соответствии с 152-ФЗ.",
        [
            "перечень обрабатываемых данных: email, имя, дата рождения, IP-адрес, "
            "данные, которые сообщает организатор при подаче заявки;",
            "цели, правовые основания и способы обработки;",
            "сроки хранения и порядок уничтожения данных;",
            "права субъекта персональных данных и порядок обращения к оператору;",
            "сведения о передаче данных третьим лицам (хостинг, аналитика, OAuth-провайдеры).",
        ],
    ),
    (
        "cookies",
        "Политика cookie",
        "Какие файлы cookie и записи локального хранилища использует сайт.",
        [
            "перечень используемых cookie и ключей localStorage с назначением и сроком;",
            "разделение обязательных и аналитических cookie;",
            "как отказаться от cookie и что при этом перестанет работать;",
            "сведения об обезличенной статистике посещений;",
            "порядок обновления политики при изменении состава cookie.",
        ],
    ),
    (
        "recommendations",
        "Правила применения рекомендательных технологий",
        "Правила в соответствии с требованиями к рекомендательным системам.",
        [
            "какие рекомендательные алгоритмы применяются: подборки, «похожие события», "
            "порядок выдачи в ленте;",
            "какие данные используются для подбора и какие не используются;",
            "как пользователь может отказаться от персонализации;",
            "сроки и порядок обновления правил.",
        ],
    ),
]


def _register_fonts() -> tuple[str, str, str]:
    """Arial с Windows, DejaVu как запасной вариант (Linux/CI)."""
    candidates = [
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            pdfmetrics.registerFont(TTFont("DocSans", str(regular)))
            pdfmetrics.registerFont(TTFont("DocSans-Bold", str(bold)))
            return "DocSans", "DocSans-Bold", str(regular)
    raise SystemExit("Не найден TTF-шрифт с кириллицей (Arial или DejaVu Sans)")


def _styles(font: str, bold: str) -> dict[str, ParagraphStyle]:
    return {
        "brand": ParagraphStyle(
            "brand", fontName=bold, fontSize=9, leading=11, textColor=MUTED,
            spaceAfter=2, alignment=TA_LEFT,
        ),
        "title": ParagraphStyle(
            "title", fontName=bold, fontSize=25, leading=30, textColor=INK,
            spaceBefore=10, spaceAfter=6,
        ),
        "sub": ParagraphStyle(
            "sub", fontName=font, fontSize=11.5, leading=17, textColor=MUTED,
            spaceAfter=14,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=bold, fontSize=12.5, leading=16, textColor=INK,
            spaceBefore=16, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body", fontName=font, fontSize=10.5, leading=16, textColor=INK,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName=font, fontSize=10.5, leading=16, textColor=INK,
            leftIndent=14, bulletIndent=2, spaceAfter=3,
        ),
        "note": ParagraphStyle(
            "note", fontName=font, fontSize=10, leading=15, textColor=INK,
        ),
        "small": ParagraphStyle(
            "small", fontName=font, fontSize=8.5, leading=12, textColor=MUTED,
        ),
    }


def _page_furniture(font: str, label: str):
    def draw(canvas, doc):  # noqa: ANN001
        canvas.saveState()
        width, height = A4
        # Полоса «черновик» — чтобы файл нельзя было принять за опубликованный документ.
        canvas.setFillColor(DANGER_BG)
        canvas.rect(0, height - 12 * mm, width, 12 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#b53c37"))
        canvas.setFont(font, 8)
        canvas.drawString(
            20 * mm, height - 7.6 * mm,
            "ЧЕРНОВИК · ТЕХНИЧЕСКАЯ ЗАГЛУШКА · НЕ ДЛЯ ПУБЛИКАЦИИ",
        )
        canvas.setFillColor(MUTED)
        canvas.setFont(font, 7.5)
        canvas.drawString(20 * mm, 12 * mm, OPERATOR)
        canvas.drawRightString(width - 20 * mm, 12 * mm, f"{label} · стр. 1")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 16 * mm, width - 20 * mm, 16 * mm)
        canvas.restoreState()

    return draw


def build_doc(slug: str, title: str, purpose: str, bullets: list[str]) -> Path:
    regular, bold, _ = _register_fonts()
    st = _styles(regular, bold)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{slug}-draft.pdf"

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=22 * mm,
        title=f"{title} — чотамзавтра.рф (черновик)",
        author="чотамзавтра.рф",
        subject="Черновик документа",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([
        PageTemplate(id="main", frames=[frame], onPage=_page_furniture(regular, title))
    ])

    story: list = [
        Paragraph("ЧТО У НАС ТАМ ЗАВТРА · чотамзавтра.рф", st["brand"]),
        HRFlowable(width="100%", thickness=1, color=LINE, spaceBefore=4, spaceAfter=2),
        Paragraph(title, st["title"]),
        Paragraph(purpose, st["sub"]),
    ]

    notice = Table(
        [[Paragraph(
            "<b>Это заглушка, а не действующая редакция.</b> Файл размещён, чтобы "
            "ссылки на документ в интерфейсе сайта вели на существующий файл. "
            "Полный текст документа находится в финальной вычитке и будет "
            "опубликован до запуска сайта. Настоящий файл не является офертой, "
            "не порождает правовых последствий и подлежит замене.",
            st["note"],
        )]],
        colWidths=[doc.width],
    )
    notice.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DANGER_BG),
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#f3c1bd")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [notice, Paragraph("Что будет в документе", st["h2"])]
    story += [Paragraph(text, st["bullet"], bulletText="—") for text in bullets]

    story += [
        Paragraph("Реквизиты оператора", st["h2"]),
        Paragraph(
            "Осмольский Егор Александрович<br/>"
            "ИНН: 422193686126<br/>"
            "Статус: самозанятый (НПД)<br/>"
            "Город: Новокузнецк<br/>"
            "Почта для обращений: chetamzavtra@ya.ru",
            st["body"],
        ),
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.5, color=LINE, spaceBefore=6, spaceAfter=6),
        Paragraph(
            "Редакция документа будет опубликована по адресу "
            f"<font color='#0099ff'>чотамзавтра.рф/{slug}</font>. "
            "До этого момента действует предыдущая опубликованная редакция либо, "
            "если её нет, документ считается не вступившим в силу.",
            st["small"],
        ),
    ]

    doc.build(story)
    return out_path


def main() -> None:
    for slug, title, purpose, bullets in DOCS:
        path = build_doc(slug, title, purpose, bullets)
        print(f"{path.relative_to(ROOT)}  ({path.stat().st_size} байт)")


if __name__ == "__main__":
    main()
