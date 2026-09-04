# Predictive Query Optimization

Самостоятельная система прогнозирования времени выполнения SQL-запросов в
PostgreSQL. Проект извлекает структурные признаки SQL и оценочные признаки
`EXPLAIN`, после чего локальная модель XGBoost прогнозирует время выполнения,
не запуская пользовательский запрос.

## Текущее состояние

- PostgreSQL 17 запускается в Docker Compose;
- предметная область содержит 8 связанных авиационных сущностей;
- генератор формирует 52 структурных типа запросов;
- обучающий набор содержит 10 400 измерений и 3 398 уникальных SQL;
- XGBoost-модель достигает `R² = 0,9661`, `MAE = 12,52 мс` на запросах до
  3 секунд и stress-R² `0,9582` на полностью новых структурах;
- DQN выбирает лучшее измеренное действие в `62,5%` случаев против `28,6%`
  случайно, а на новых структурах — в `41,6%` против `23,4%`;
- SQL, планы, прогнозы и рекомендации журналируются в служебной схеме `pqo`;
- PyQt6-приложение объединяет анализ SQL, историю, настройки
  PostgreSQL, графики экспериментов, экспорт и запуск обучения;
- сбор данных, обучение, экспорт и прогноз доступны как Python API и CLI;
- индексные действия формируются из SQL и безопасно оцениваются с откатом DDL;
- модульные и интеграционные тесты проверяют ключевые компоненты.

## Архитектура

```text
PostgreSQL -> EXPLAIN + catalog statistics -> feature vector -> XGBoost/DQN
     |                                                           |
     +--------------------- schema pqo <--------------------------+
```

- `src/pqo` — прикладная логика на Python;
- `database/init` — схема, функции, триггеры и тестовые данные PostgreSQL;
- `dataset/postgresql` — воспроизводимый обучающий набор;
- `models/xgboost` — выбранная модель и метрики;
- `models/dqn` — агент индексных рекомендаций и два отчёта оценки;
- `tests` — модульные и интеграционные тесты;
- `docs` — описание БД, датасета и экспериментов.

## Быстрый запуск

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:PQO_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest
```

PostgreSQL доступен на порту `55432`, чтобы не конфликтовать с локальным
экземпляром на стандартном порту `5432`.

Запустить десктопное приложение:

```powershell
.\.venv\Scripts\pqo-desktop.exe
```

Отдельная установка только компонентов приложения доступна командой
`.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"`.

## Работа с данными и моделью

Сгенерировать и измерить сбалансированную выборку:

```powershell
.\.venv\Scripts\pqo-generate.exe 5200 artifacts\aviation_dataset.csv --seed 4242 --repetitions 2
```

Обучить модель:

```powershell
.\.venv\Scripts\pqo-train.exe dataset\postgresql\aviation_dataset.csv artifacts\models\experiment
```

Получить прогноз без выполнения SQL-запроса:

```powershell
.\.venv\Scripts\pqo-predict.exe models\xgboost\xgboost_query_time.joblib "SELECT * FROM aviation.flights WHERE departure_airport = 'MSQ'"
```

Получить единый прогноз XGBoost и рекомендацию DQN с сохранением в `pqo`:

```powershell
.\.venv\Scripts\pqo-analyze.exe models\xgboost\xgboost_query_time.joblib models\dqn\dqn_index_advisor.pt "SELECT * FROM aviation.flights WHERE departure_airport = 'MSQ'" --threshold-ms 50
```

Собрать признаки для списка запросов, по одному запросу в строке:

```powershell
.\.venv\Scripts\pqo-collect.exe tests\fixtures\smoke_queries.txt artifacts\smoke.csv
```

## Документация

- [Модель данных](docs/database-model.md)
- [Проектирование датасета](docs/dataset-design.md)
- [Разработка и оценка XGBoost](docs/xgboost-baseline.md)
- [Среда индексных рекомендаций](docs/index-recommendation-environment.md)
- [Самостоятельное обучение и проверка моделей](docs/training-guide.md)
- [Десктопное PyQt6-приложение](docs/desktop-application.md)
- [Production-like контрольный benchmark](docs/production-control-benchmark.md)

## Следующие этапы

1. Проверка переносимости на дополнительной предметной базе.
2. Упаковка приложения и подготовка материалов курсовой работы.
