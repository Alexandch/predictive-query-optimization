# Predictive Query Optimization

Самостоятельная система прогнозирования времени выполнения SQL-запросов в
PostgreSQL. Проект извлекает структурные признаки SQL и оценочные признаки
`EXPLAIN`, после чего локальная модель XGBoost прогнозирует время выполнения,
не запуская пользовательский запрос.

## Текущее состояние

- PostgreSQL 17 запускается в Docker Compose;
- четыре предметные области содержат 8 авиационных, 10 розничных,
  10 логистических и 12 CH-benCHmark-совместимых сущностей;
- обучающие генераторы формируют 90 структурных типов запросов в трёх train-доменах;
- набор выбранной XGBoost-модели содержит 13 760 измерений и 6 474 уникальных SQL;
- многодоменная XGBoost-модель достигает `R² = 0,9710`, `MAE = 13,81 мс` на
  parameter-holdout и stress-R² `0,8655` на полностью новых структурах;
- DQN обучена на 5 350 действиях и 86 шаблонах; отрицательные примеры подняли
  accuracy и снизили regret на независимых контролях всех трёх БД;
- экспериментальное CH-расширение добавляет 800 измерений XGBoost и 300
  действий DQN; кандидаты честно отклонены после регрессии внешнего CH/logistics
  контроля, поэтому выбранные основные модели не перезаписаны;
- на полностью исключённой из обучения схеме `logistics` XGBoost достигает
  `R² = 0,8636`, а DQN снижает mean regret с `0,2557` случайной стратегии до `0,0603`;
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

Для Windows также подготовлен автономный установщик. Его сборка включает
Python, PyQt6, XGBoost, DQN-модель и необходимые библиотеки:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

После установки приложение запускается ярлыком «Predictive Query Optimization»
на рабочем столе или из меню «Пуск». Python для обычного запуска не требуется;
PostgreSQL по-прежнему должен быть доступен по параметрам на вкладке «Настройки».

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
- [Сборка и установка в Windows](docs/windows-packaging.md)
- [Production-like контрольный benchmark](docs/production-control-benchmark.md)
- [Многодоменное обучение и проверка переносимости](docs/multidomain-experiment.md)
- [Строгая проверка leave-one-database-out](docs/leave-one-database-out.md)
- [Расширение DQN отрицательными примерами](docs/dqn-negative-augmentation.md)
- [Расширение нагрузки на основе CH-benCHmark](docs/chbenchmark-experiment.md)
- [Эксперимент с балансировкой шаблонов](docs/chbenchmark-balanced-experiment.md)

## Следующие этапы

1. Подготовка текста, диаграмм и приложений курсовой работы.
