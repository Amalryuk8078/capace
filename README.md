# Smart Task Board

A task board API built with Django REST Framework, PostgreSQL and Redis. Tasks can be created and
completed, but completion is controlled by a set of business rules that are enforced on the server.
A simple HTML board page is included for using the API from the browser.

## Features

- Create, list and view tasks with a priority (`LOW`, `MEDIUM`, `HIGH`) and an estimated time
- Deadlines calculated by the server
- Burst protection: creating too many tasks in a short time locks the next one
- Completion rules checked on the server, with cryptic hints when a completion fails
- Board summary counts (total, pending, completed, locked, expired)
- Swagger UI and OpenAPI schema
- Browser task board with live deadline and lock countdowns

## Tech stack

- Python, Django 5.2, Django REST Framework
- PostgreSQL 16
- Redis (redis-stack, includes the RedisInsight web UI)
- drf-spectacular for API docs
- pytest, fakeredis and freezegun for tests
- Docker Compose

## Business rules

| # | Rule | What it does |
|---|---|---|
| 1 | High-priority dependency | A `HIGH` task can only be completed once at least one `LOW` task has been completed. |
| 2 | Burst lock | If 3 tasks have been created in the last 2 minutes, the next task is locked for 5 minutes. |
| 3 | Deadline | Created on an odd minute: deadline = created time + estimated time. Created on an even minute: created time + 2 × estimated time. |
| 4 | Hidden eligibility | Some tasks can never be completed. This is decided from a secret key and the task ID (about 80% are eligible by default). |
| 5 | Cryptic hints | A failed completion returns `409` with a hint code and message instead of the real reason. |

Expired or locked tasks cannot be completed either. Each failed attempt increases the task's
`failure_count`.

## Getting started

### Requirements

- Docker and Docker Compose

### Run with Docker

```bash
cp .env.example .env    # then set real secrets and passwords
docker compose -p capace up -d --build
```

Migrations run automatically when the backend starts.

| Page | URL |
|---|---|
| Task board | http://localhost:8765/ |
| Swagger UI | http://localhost:8765/api/docs/ |
| OpenAPI schema | http://localhost:8765/api/schema/ |
| Browsable API | http://localhost:8765/api/tasks/ |
| RedisInsight | http://localhost:8001/ |

### Run locally (without the backend container)

```bash
docker compose -p capace up -d postgres redis
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py runserver
```

Settings are read from the `.env` file in the repository root.

## Configuration

All settings live in `.env` (see [.env.example](.env.example)).

| Variable | Description |
|---|---|
| `DJANGO_DEBUG` | Enable debug mode |
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hosts |
| `DJANGO_TIME_ZONE` | Time zone used for the odd/even minute rule |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Database credentials |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Database address (host side) |
| `REDIS_URL` | Redis connection URL |
| `REDIS_EXPOSED_PORT` | Host port for RedisInsight |
| `TASK_ELIGIBILITY_SECRET` | Secret used for the hidden eligibility rule |
| `TASK_ELIGIBILITY_RATIO` | Share of tasks that are eligible (default `0.8`) |

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/tasks/` | List tasks |
| `POST` | `/api/tasks/` | Create a task |
| `GET` | `/api/tasks/{id}/` | Get one task |
| `POST` | `/api/tasks/{id}/complete/` | Try to complete a task |
| `GET` | `/api/board/summary/` | Board summary counts |

### Create a task

```bash
curl -X POST http://localhost:8765/api/tasks/ \
  -H "Content-Type: application/json" \
  -d '{"title": "Write report", "priority": "LOW", "estimated_time_minutes": 30}'
```

### Failed completion example

```json
{
  "success": false,
  "message": "The path remains closed.",
  "hint_code": "HINT_03"
}
```

| Status | Meaning |
|---|---|
| `201` | Task created |
| `200` | Task completed |
| `404` | Task not found |
| `409` | Completion rejected (see hint) |
| `503` | Redis unavailable, task not created |

## Running tests

```bash
cd backend
pytest
```

Tests need PostgreSQL running (`docker compose -p capace up -d postgres`). Redis is replaced with
fakeredis.

## Project structure

```text
.
├── docker-compose.yml
├── .env.example
└── backend/
    ├── Dockerfile
    ├── manage.py
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── config/              settings, URLs, error handler
    └── tasks/
        ├── models.py        Task model
        ├── serializers.py   request validation and responses
        ├── views.py         API views and the board page
        ├── services.py      business rules
        ├── urls.py
        ├── templates/tasks/board.html
        └── tests.py
```
