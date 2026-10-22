"""Собирает иконки бренда из геометрии знака в docs/index.html (`.brand-mark`).

Знак — это скруглённый квадрат 34x34 (radius 11), из которого в правый верхний
угол «вываливается» акцентный круг 26px, а в левом нижнем сидит белая точка 13px.
Скрипт пересчитывает те же пропорции на любой размер и отдаёт PNG.

    python backend/build_brand_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "brand"

DEEP = (11, 26, 51, 255)  # --deep светлой темы, #0b1a33
ACCENT = (0, 153, 255, 255)  # --accent, #0099ff
WHITE = (255, 255, 255, 255)

BOX = 34.0  # сторона знака в CSS, px
SS = 4  # суперсэмплинг


def render(size: int, rounded: bool) -> Image.Image:
    big = size * SS
    k = big / BOX
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, big, big], fill=DEEP)

    r1, c1 = 13 * k, (big - 6 * k, 6 * k)
    d.ellipse([c1[0] - r1, c1[1] - r1, c1[0] + r1, c1[1] + r1], fill=ACCENT)

    r2, c2 = 6.5 * k, (13.5 * k, big - 13.5 * k)
    d.ellipse([c2[0] - r2, c2[1] - r2, c2[0] + r2, c2[1] + r2], fill=WHITE)

    if rounded:  # как overflow:hidden у .brand-mark: круги обрезаются скруглением
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, big, big], radius=11 * k, fill=255)
        img.putalpha(mask)

    return img.resize((size, size), Image.LANCZOS)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <clipPath id="mark"><rect width="512" height="512" rx="165.6"/></clipPath>
  </defs>
  <g clip-path="url(#mark)">
    <rect width="512" height="512" fill="#0b1a33"/>
    <circle cx="421.6" cy="90.4" r="195.8" fill="#0099ff"/>
    <circle cx="203.3" cy="421.7" r="97.9" fill="#fff"/>
  </g>
</svg>
"""

COVER_W, COVER_H = 1200, 630
FONT_DIRS = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    raise SystemExit(f"нет шрифта {name} — положи его в {FONT_DIRS[0]}")


def render_cover() -> Image.Image:
    """Обложка 1200x630 для og:image: знак, словесный логотип, город."""
    img = Image.new("RGB", (COVER_W, COVER_H), DEEP[:3])
    d = ImageDraw.Draw(img)

    # акцентный круг уходит за правый край — как в знаке
    d.ellipse([COVER_W - 380, -260, COVER_W + 420, 540], fill=ACCENT[:3])

    # знак в левом верхнем углу
    mark = render(96, rounded=True)
    img.paste(mark, (80, 72), mark)

    # словесный логотип: подбираем кегль так, чтобы не залезть на круг
    max_w = COVER_W - 380 - 80 - 40
    size = 104
    while size > 60:
        f = font("Montserrat-Bold.ttf", size)
        if max(d.textlength("что у нас", font=f), d.textlength("там завтра", font=f)) <= max_w:
            break
        size -= 4
    f = font("Montserrat-Bold.ttf", size)
    y = 250
    for line in ("что у нас", "там завтра"):
        d.text((80, y), line, font=f, fill=WHITE[:3])
        y += int(size * 1.04)

    f2 = font("OpenSans-Regular.ttf", 30)
    d.text((84, y + 26), "ГОРОДСКАЯ АФИША · НОВОКУЗНЕЦК", font=f2, fill=(157, 196, 232))
    return img



def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("app-icon-512.png", 512, False),
        ("app-icon-1024.png", 1024, False),
        ("brand-mark-512.png", 512, True),
        ("brand-mark-192.png", 192, True),
    ]
    for name, size, rounded in jobs:
        img = render(size, rounded)
        img.save(OUT / name, optimize=True)
        print(f"{name}: {size}x{size}, {(OUT / name).stat().st_size / 1024:.1f} KB")
    (OUT / "brand-mark.svg").write_text(SVG, encoding="utf-8")
    print("brand-mark.svg: вектор")

    cover = OUT / "og-cover.png"
    render_cover().save(cover, optimize=True)
    print(f"og-cover.png: {COVER_W}x{COVER_H}, {cover.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
