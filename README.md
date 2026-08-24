# Что у нас там завтра — Городская афиша

MVP-проект городской афиши событий для Новокузнецка. Backend на FastAPI + SQLite, Frontend — одностраничный HTML/CSS/JS.

## Структура проекта

```
Web-Afisha-Kaktus/
├── backend/                 # FastAPI приложение
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # Основное приложение и API эндпоинты
│   │   ├── models.py        # SQLAlchemy модели
│   │   └── db.py            # Настройка БД
│   ├── tests/
│   │   └── test_api.py
│   └── requirements.txt
├── frontend/
│   └── index.html           # SPA фронтенд
├── afisha.sqlite3           # SQLite база данных (создаётся автоматически)
├── .gitignore
└── README.md
```

## Быстрый старт

### 1. Backend (FastAPI)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

API будет доступен на `http://127.0.0.1:8000`

- Документация: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/api/v1/health`

### 2. Frontend

Просто откройте `frontend/index.html` в браузере **или** раздайте через любой статический сервер:

```bash
cd frontend
python -m http.server 4173
```

Затем откройте `http://127.0.0.1:4173` — фронтенд настроен на работу с API на порту 8000 (CORS разрешен для 4173).

## API эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/v1/health` | Проверка здоровья API |
| POST | `/api/v1/events` | Создать событие (на модерации) |
| GET | `/api/v1/events` | Список опубликованных событий (с фильтрами) |
| GET | `/api/v1/events/calendar` | События для календаря за период |
| GET | `/api/v1/events/{slug}` | Получить событие по slug |

### Параметры фильтрации для `/api/v1/events`

- `search` — поиск по названию, категории, месту
- `category` — фильтр по категории
- `date_from` — дата от (YYYY-MM-DD)
- `date_to` — дата до (YYYY-MM-DD)
- `sort` — `date` (по умолчанию) или `title`

## Разработка

### Запуск тестов

```bash
cd backend
pytest -v
```

### Структура БД

Таблица `events`:
- `id` — PK
- `title` — название
- `slug` — URL-slug (уникальный)
- `status` — `pending_moderation` | `published` | `rejected`
- `category` — категория
- `date` — дата события
- `time` — время начала
- `venue` — место проведения
- `price` — стоимость (строка)
- `created_at` — дата создания

## Git workflow

```bash
# Создать фичу
git checkout -b feature/nazvanie-fichi

# Коммитить изменения
git add .
git commit -m "feat: описание изменения"

# Откатить к предыдущему коммиту (мягко — изменения остаются в рабочей директории)
git reset --soft HEAD~1

# Жёсткий откат (удалить изменения)
git reset --hard HEAD~1

# Посмотреть историю
git log --oneline -10

# Создать релизный тег
git tag -a v0.1.0 -m "MVP: каталог, поиск, календарь, модерация"
git push origin v0.1.0
```

## Планы (MVP → Roadmap)

См. `.hermes/plans/` — подробные планы разработки.