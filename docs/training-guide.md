# Обучение и оценка моделей

Рабочие веса и их назначение перечислены в [models/README.md](../models/README.md).
Новую модель сохраняйте в отдельный каталог. Сбор времён и индексного опыта
выполняет запросы; для него нужен подготовленный тестовый PostgreSQL.

## Окружение

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Подключение задаётся [переменными окружения](connections.md). Python-команды
не читают `.env` автоматически. Для новой локальной БД подготовьте Docker-стенд
по [README](../README.md); порядок тестов описан в [testing.md](testing.md).
После обновления исходников повторите установку пакета, чтобы обновить точки входа CLI.

## Сбор данных для времени выполнения

В CSV сохраняются `template_id`, исходный SQL, признаки AST, оценочного плана
и каталога БД, а также измеренное время. Значения Actual используются как цель
и диагностика, а не как доступные до выполнения входы прогноза.

```powershell
.\.venv\Scripts\pqo-generate.exe 5200 artifacts\training\aviation.csv --seed 4242 --repetitions 2 --no-persist
.\.venv\Scripts\pqo-generate.exe 5200 artifacts\training\aviation.csv --seed 4242 --repetitions 2 --no-persist --resume
```

Второй вызов нужен только для продолжения того же незавершённого запуска.
Для собственных семейств заполните [манифест](../dataset/query_manifest.example.csv):

```powershell
.\.venv\Scripts\pqo-collect.exe dataset\query_manifest.example.csv artifacts\training\custom.csv
.\.venv\Scripts\pqo-merge.exe artifacts\training\combined.csv artifacts\training\aviation.csv artifacts\training\custom.csv
```

`pqo-collect` и `pqo-export` могут использовать служебную историю в БД текущего
CLI-подключения. Проверьте назначение подключения и `--help` перед сбором.
Разные версии схемы признаков нельзя объединять только из-за одинакового расширения CSV.

## XGBoost

Для свежего набора с полями plan-v1:

```powershell
.\.venv\Scripts\pqo-train.exe artifacts\training\combined.csv artifacts\models\xgb-candidate --feature-version plan-v1 --target-transform identity --seed 42
.\.venv\Scripts\pqo-predict.exe artifacts\models\xgb-candidate\xgboost_query_time.joblib "SELECT flight_id FROM aviation.flights WHERE departure_airport = 'MSQ'"
```

Поддерживаются контракты `legacy`, `sql-v2`, `sql-v3`, `plan-v1`, `plan-v2` и
преобразования цели identity/log1p. Выбирайте контракт, представленный в данных;
старый CSV не становится совместимым от добавления нулевых столбцов.

Повторы точного SQL агрегируются медианой. Parameter-holdout проверяет новые
параметры известных семейств, unseen-template — целые отложенные семейства.
Сравнивайте MAE/RMSE, Q-error и ошибки по доменам/семействам, а не только R².
Кандидат и действующая модель должны оцениваться на одних SQL и измерениях.

## Индексные модели

Сбор DQN-опыта создаёт индексы в откатываемых транзакциях:

```powershell
.\.venv\Scripts\pqo-dqn-collect.exe 1040 artifacts\training\dqn.jsonl --actions-per-query 3 --repetitions 1 --seed 2027 --resume
.\.venv\Scripts\pqo-dqn-train.exe artifacts\training\dqn.jsonl artifacts\models\dqn-candidate --epochs 2000 --batch-size 128 --learning-rate 0.001 --ranking-weight 0.10 --seed 42
```

Последовательный опыт хранит переходы состояния после каждого принятого шага:

```powershell
.\.venv\Scripts\pqo-sequential-collect.exe aviation 100 artifacts\training\sequential.jsonl --max-steps 2 --budget-mb 64 --seed 42
.\.venv\Scripts\pqo-sequential-train.exe artifacts\training\sequential.jsonl artifacts\models\sequential-candidate --seed 42
```

При объединении используйте соответствующие `pqo-merge-dqn` или
`pqo-sequential-merge`. Кодировки действий и состояний должны совпадать.
В метриках учитывайте regret, сравнение с NOOP/Random, неиспользованные индексы
и ошибки по доменам. Дополнительные отрицательные примеры собирает `pqo-dqn-negative`;
измеренный reward сохраняется отдельно от нормализованной обучающей награды.

## Разделение обучения и контроля

- фиксируйте seed, версии весов, набор признаков и состав разбиений до оценки;
- не включайте control/zero-shot наборы и связанные SQL-семейства в обучение;
- выбирайте параметры по validation, итоговый контроль используйте после выбора;
- не называйте повторную настройку по просмотренному контролю независимой оценкой;
- сравнивайте первый прогноз без калибровки отдельно от оценок по прежним замерам.

Роли существующих файлов указаны в [описании датасетов](../dataset/postgresql/README.md).
Доменные генераторы, контроль и офлайн-ранжирование переписываний собраны в
[experiments.md](experiments.md). Измерения другого оборудования или кэша
не следует смешивать как взаимозаменяемые цели без проверки сдвига времени.

## Подключение кандидата

В расширенном режиме приложение сохраняет результат обучения отдельно и
проверяет критерии замены. Перед переключением сохраняются прежние веса.
При ручном подключении проверьте совместимость признаков и независимый контроль;
ошибка отсутствующих признаков не исправляется их заполнением нулями.
Смена весов делает прежний калибровочный профиль неприменимым к новой модели.
