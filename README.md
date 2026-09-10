# Predictive Query Optimization

Самостоятельная система прогнозирования времени выполнения SQL-запросов в
PostgreSQL. Проект извлекает структурные признаки SQL и оценочные признаки
`EXPLAIN`, после чего локальная модель XGBoost прогнозирует время выполнения,
не запуская пользовательский запрос.

## Текущее состояние

- PostgreSQL 17 запускается в Docker Compose;
- пять предметных областей содержат 8 авиационных, 10 розничных,
  10 логистических, 12 CH-benCHmark-совместимых и 15 основных Pagila-сущностей;
- обучающие генераторы формируют 120 структурных типов запросов в четырёх
  development/train-доменах;
- набор выбранной XGBoost-модели содержит 13 760 измерений и 6 474 уникальных SQL;
- многодоменная XGBoost-модель достигает `R² = 0,9701`, `MAE = 14,18 мс` на
  parameter-holdout и stress-R² `0,7761` на полностью новых структурах;
- DQN обучена на 5 350 действиях и 86 шаблонах; отрицательные примеры подняли
  accuracy и снизили regret на независимых контролях всех трёх БД;
- экспериментальное CH-расширение добавляет 800 измерений XGBoost и 300
  действий DQN; кандидаты честно отклонены после регрессии внешнего CH/logistics
  контроля, поэтому выбранные основные модели не перезаписаны;
- development-only проверка доли Pagila выбрала 10% по macro-ошибке трёх
  доменов, но XGBoost-кандидаты не прошли внутренний unseen-template шлюз;
- отдельная DQN-проверка выбрала полный Pagila-набор; `generic-v3` улучшила
  внутренние accuracy/regret, но отклонена после регрессии внешних контролей;
- на полностью исключённой из обучения схеме `logistics` XGBoost достигает
  `R² = 0,8636`, а DQN снижает mean regret с `0,2557` случайной стратегии до `0,0603`;
- SQL, планы, прогнозы и рекомендации журналируются в служебной схеме `pqo`;
- PyQt6-приложение объединяет анализ SQL, историю, настройки
  PostgreSQL, графики экспериментов, экспорт и запуск обучения;
- локальная калибровка под пользовательскую БД корректирует время отдельно для
  знакомых шаблонов SQL и не переносит поправку на неизвестную структуру;
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
- [Второй hard-negative эксперимент DQN](docs/dqn-hard-negative-v2.md)
- [Фиксация и проверка baseline](docs/training-baseline.md)
- [Расширение нагрузки на основе CH-benCHmark](docs/chbenchmark-experiment.md)
- [Эксперимент с балансировкой шаблонов](docs/chbenchmark-balanced-experiment.md)
- [Калибровка прогноза для пользовательской БД](docs/database-calibration.md)
- [Экспериментальная проверка калибровки](docs/calibration-experiment.md)
- [Development-эксперимент Pagila и generic-v3](docs/pagila-development-experiment.md)
- [Domain-balanced подбор доли Pagila](docs/domain-balanced-validation.md)
- [Domain-balanced validation DQN generic-v3](docs/dqn-domain-balanced-validation.md)
- [Резервное копирование и восстановление](docs/backup-and-recovery.md)

## Следующие этапы

1. Последовательная multi-index среда DQN с действием `STOP` и бюджетом.
2. Обучение последовательного агента и проверка против однократной DQN.
3. Подготовка текста, диаграмм и приложений курсовой работы.
