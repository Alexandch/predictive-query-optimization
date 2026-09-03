# Predictive Query Optimization

Самостоятельная система прогнозирования времени выполнения SQL-запросов в
PostgreSQL. Проект извлекает структурные признаки SQL и оценочные признаки
`EXPLAIN`, после чего локальная модель XGBoost прогнозирует время выполнения,
не запуская пользовательский запрос.

## Текущее состояние

- PostgreSQL 17 запускается в Docker Compose;
- предметная область содержит 8 связанных авиационных сущностей;
- генератор формирует 24 структурных типа запросов;
- обучающий набор содержит 2 400 реальных измерений;
- лучшая XGBoost-модель достигает `R² = 0,9858` и `MAE = 2,44 мс`;
- DQN выбирает лучшее измеренное индексное действие в `59,8%` случаев против
  `24,4%` у случайной стратегии на основном holdout;
- SQL, планы, прогнозы и рекомендации журналируются в служебной схеме `pqo`;
- сбор данных, обучение, экспорт и прогноз доступны как Python API и CLI;
- индексные действия формируются из SQL и безопасно оцениваются с откатом DDL;
- модульные и интеграционные тесты проверяют ключевые компоненты.

## Архитектура

```text
PostgreSQL -> EXPLAIN JSON -> SQL/plan features -> XGBoost -> prediction
     |                                                   |
     +---------------- schema pqo <----------------------+
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

## Работа с данными и моделью

Сгенерировать и измерить сбалансированную выборку:

```powershell
.\.venv\Scripts\pqo-generate.exe 2400 artifacts\aviation_dataset.csv --seed 42
```

Обучить модель:

```powershell
.\.venv\Scripts\pqo-train.exe dataset\postgresql\aviation_dataset.csv artifacts\models\experiment
```

Получить прогноз без выполнения SQL-запроса:

```powershell
.\.venv\Scripts\pqo-predict.exe models\xgboost\xgboost_query_time.joblib "SELECT * FROM aviation.flights WHERE departure_airport = 'MSQ'"
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

## Следующие этапы

1. Расширение XGBoost и DQN новыми сложными структурными шаблонами.
2. Прикладной сервис, объединяющий прогноз XGBoost и рекомендации DQN.
3. Десктопное приложение PyQt с анализом, историей, настройками и обучением.
4. Упаковка приложения и подготовка материалов курсовой работы.
