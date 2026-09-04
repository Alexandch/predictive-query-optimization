# Контрольный production-like benchmark

## Цель

Контрольная выборка измеряет обобщающую способность уже обученных XGBoost и DQN.
Её запросы не используются для обучения, подбора гиперпараметров или выбора чекпоинта.

## Происхождение нагрузки

Набор состоит из 30 новых параметризованных структур. Они не копируют SQL сторонних
систем, а адаптируют к авиационной схеме типовые production- и decision-support-паттерны:

- многотабличные агрегации, коррелированные semi/anti joins и сверки данных;
- `LATERAL`, `EXISTS`, `INTERSECT`, `EXCEPT`, `CUBE` и `MATERIALIZED` CTE;
- рекурсивные маршруты и календарные ряды;
- ranking, top-N-per-group, rolling windows, `LAG`, `NTILE`, `PERCENT_RANK` и `CUME_DIST`;
- воронки, когорты, аудит целостности, выбросы и распределения.

Методика опирается на [TPC-H](https://www.tpc.org/tpch/) с 22 decision-support-запросами,
документацию PostgreSQL по [оконным функциям](https://www.postgresql.org/docs/current/functions-window.html),
[CTE](https://www.postgresql.org/docs/current/queries-with.html),
[`LATERAL`](https://www.postgresql.org/docs/current/queries-table-expressions.html) и
[`EXPLAIN ANALYZE`](https://www.postgresql.org/docs/current/using-explain.html).
В production приоритет запросов обычно определяют по `calls`, `total_exec_time`, `mean_exec_time`
и буферному I/O из
[`pg_stat_statements`](https://www.postgresql.org/docs/current/pgstatstatements.html).

## Воспроизведение

```powershell
# 300 параметризаций, по 2 замера каждой
.\.venv\Scripts\pqo-production-benchmark.exe collect-xgb 300 artifacts\production-control\xgboost_control.csv --repetitions 2

# 60 запросов, NOOP + 2 индексных кандидата
.\.venv\Scripts\pqo-production-benchmark.exe collect-dqn 60 artifacts\production-control\dqn_control.jsonl --actions-per-query 2

# Оценка замороженных моделей
.\.venv\Scripts\pqo-production-benchmark.exe evaluate `
  artifacts\production-control\xgboost_control.csv `
  models\xgboost\xgboost_query_time.joblib `
  artifacts\production-control\results `
  --dqn-experience artifacts\production-control\dqn_control.jsonl `
  --dqn-model models\dqn\dqn_index_advisor.pt
```

DDL DQN-экспериментов выполняется в транзакции и всегда откатывается. Основные индексы БД
не изменяются.

## Результаты

Прогон выполнен 4 сентября 2026 года на PostgreSQL 17.11. XGBoost-часть содержит
600 измерений 171 уникального SQL из 30 новых структур. Каждый SQL замерялся
дважды, после чего бралась медиана.

| Метрика XGBoost | Значение |
|---|---:|
| R² | 0,3758 |
| MAE | 637,76 мс |
| Median AE | 95,39 мс |
| P90 AE | 1 846,83 мс |
| RMSE | 1 387,65 мс |
| MAPE | 55,32% |
| Ошибка не более 20% | 20,47% |
| Диапазон фактического времени | 7,75–8 118,33 мс |

Наиболее трудные для XGBoost классы: тепловая карта мест (`MAE 6 062 мс`),
когортное удержание (`2 659 мс`), цепочки неявок (`2 238 мс`) и децили ценности
пассажиров (`2 213 мс`). Это многотабличные агрегации с оконными функциями, которых
недостаточно в обучающем диапазоне.

DQN проверен на 84 реально измеренных действиях. В 27 классах были доступны NOOP
и два индексных кандидата; ещё три класса не имели безопасных индексных альтернатив.

| Политика | Accuracy | Mean regret |
|---|---:|---:|
| DQN | 14,81% | 0,1093 |
| Random | 37,04% | 0,1275 |
| NOOP | 22,22% | 0,1501 |

DQN реже выбирает точно лучшее действие, чем обе базовые политики. Однако его средний regret
ниже: ошибочные решения агента в среднем менее вредны. Этот результат не подтверждает
переносимость DQN и обосновывает расширение его обучающего опыта.

Машиночитаемые отчёты находятся в `models/control`, а воспроизводимые исходные замеры —
в `dataset/postgresql/production_control.csv` и `dataset/postgresql/dqn_production_control.jsonl`.
