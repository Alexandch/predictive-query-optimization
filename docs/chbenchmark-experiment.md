# Расширение нагрузки на основе CH-benCHmark

## Назначение

Новая предметная область проверяет переносимость моделей на гибридную систему
заказов, складов и поставщиков. Структура опирается на открытые PostgreSQL DDL
проекта [CMU BenchBase](https://github.com/cmu-db/benchbase), который
распространяется по Apache License 2.0. Семантика нагрузки соответствует идее
[CH-benCHmark](https://github.com/cmu-db/benchbase/wiki/CH-benCHmark):
транзакционные данные TPC-C используются для аналитических запросов класса
TPC-H.

Это CH-benCHmark-совместимая исследовательская нагрузка, а не официальный
результат TPC/CH-benCHmark. Наполнение и parameterized SELECT-шаблоны созданы
в проекте, чтобы запуск не требовал Java и не зависел от внешних файлов.

## Схема и объём

Схема `chbenchmark` содержит 12 сущностей:

1. `region`;
2. `nation`;
3. `supplier`;
4. `warehouse`;
5. `district`;
6. `customer`;
7. `item`;
8. `stock`;
9. `oorder`;
10. `new_order`;
11. `order_line`;
12. `history`.

Детерминированное наполнение создаёт 1 156 074 строки. Наиболее крупные
отношения: 600 000 позиций заказов, 120 000 заказов, 120 000 клиентов,
120 000 складских остатков и 120 000 платежей. Составные внешние ключи
воспроизводят связь «склад — район — клиент — заказ».

Для существующего Docker volume схема устанавливается командой:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_chbenchmark.ps1
```

В новом volume файлы `040_chbenchmark_schema.sql` и
`041_seed_chbenchmark.sql` выполняются Docker Compose автоматически.

## Изоляция данных

- `CHBenchmarkQueryGenerator` содержит 20 обучающих структур;
- `CHBenchmarkControlQueryGenerator` содержит 10 других структур;
- множества `template_id` не пересекаются и проверяются тестом;
- контрольные CSV/JSONL запрещено передавать в `pqo-merge` и
  `pqo-merge-dqn`;
- logistics zero-shot также остаётся неизменным и не входит в обучение.

Контроль включает RFM-сегментацию, риск дефицита, волатильность спроса,
концентрацию поставщиков, интервалы между заказами, affinity товаров,
поиск аномалий, когорты, Pareto-анализ и сверку склада.

## Воспроизведение сбора

```powershell
$env:PQO_ALLOWED_SCHEMAS = "chbenchmark"

# 400 параметризаций, два реальных замера каждой
.\.venv\Scripts\pqo-chbenchmark.exe collect-xgb 400 `
  dataset\postgresql\chbenchmark_training.csv --repetitions 2 --seed 15101

# NOOP и два временных индексных действия
.\.venv\Scripts\pqo-chbenchmark.exe collect-dqn 100 `
  dataset\postgresql\chbenchmark_dqn_experience.jsonl `
  --actions-per-query 2 --repetitions 1 --seed 15101

# Независимые данные
.\.venv\Scripts\pqo-chbenchmark.exe collect-control-xgb 100 `
  dataset\postgresql\chbenchmark_control.csv --repetitions 2 --seed 16101
.\.venv\Scripts\pqo-chbenchmark.exe collect-control-dqn 30 `
  dataset\postgresql\chbenchmark_dqn_control.jsonl `
  --actions-per-query 2 --repetitions 1 --seed 16101
```

Сбор XGBoost выполняет `EXPLAIN ANALYZE` в read-only транзакции. DQN временно
создаёт индекс внутри транзакции и всегда откатывает DDL.

## Полученные данные

| Файл | Объём |
|---|---:|
| `chbenchmark_training.csv` | 800 измерений, 242 уникальных SQL, 20 шаблонов |
| `chbenchmark_control.csv` | 200 измерений, 60 уникальных SQL, 10 шаблонов |
| `chbenchmark_dqn_experience.jsonl` | 300 действий, 86 решений, 20 шаблонов |
| `chbenchmark_dqn_control.jsonl` | 90 действий, 26 решений, 10 шаблонов |

В XGBoost train-наборе время находится в диапазоне примерно от 26 мс до
6,53 с. В DQN train-наборе `NOOP` является лучшим действием для 18,6% решений;
166 из 200 измеренных индексных действий реально появились в плане PostgreSQL.

SHA-256:

- `chbenchmark_training.csv`:
  `1700ED6B21D1C2FFD753F92931AED5FC357375F1312284589B57F644610BA418`;
- `chbenchmark_control.csv`:
  `187EE469FC7252C3508FBA0BCECBDBF3335FB90F101EE30B9D2DE040A433A1B2`;
- `chbenchmark_dqn_experience.jsonl`:
  `22ECCEE78306BB3C7C9913C21F5805E3B4E50F520E8ABF4E4686CBB18F9CF9EF`;
- `chbenchmark_dqn_control.jsonl`:
  `0A16DD5BE0191EEAA49F83C2CD149F5FF3EF31C6A23132597A5AED31910D5DD2`.

## Эксперимент с кандидатами

Объединённый XGBoost train содержит 14 560 измерений и 6 716 уникальных SQL.
Первый tuned `log1p` кандидат достиг parameter `R² = 0,9821`, но stress
`R² = 0,4077`. Внешнее сравнение:

| Контроль | Основная R² / MAE | Кандидат R² / MAE |
|---|---:|---:|
| aviation production-like | 0,442 / 602,60 мс | **0,510 / 558,28 мс** |
| retail | −0,460 / 307,24 мс | **0,276 / 215,82 мс** |
| logistics zero-shot | **0,864 / 114,85 мс** | 0,600 / 206,61 мс |
| CH unseen-template | **−0,677 / 188,56 мс** | −0,826 / 182,42 мс |

Вариант без `log1p` оказался хуже на всех внешних контролях. Ни один кандидат
XGBoost не назначен основным.

Объединённый DQN train содержит 5 650 действий. Кандидат остановился на эпохе
601, лучшее состояние получено на эпохе 451. Его parameter accuracy — 52,63%,
mean regret — 0,0775. Внешнее сравнение:

| Контроль | Основная accuracy / regret | Кандидат accuracy / regret |
|---|---:|---:|
| aviation production-like | 40,74% / 0,0831 | 40,74% / **0,0536** |
| retail | 61,54% / 0,0607 | **73,08% / 0,0360** |
| logistics zero-shot | 50,00% / 0,0603 | **53,57% / 0,0567** |
| CH unseen-template | **30,77% / 0,1118** | 26,92% / 0,1219 |

DQN-кандидат также отклонён: улучшение трёх прежних контролей не компенсирует
ухудшение новой области, для которой добавлялся опыт. Основные артефакты в
`models/xgboost` и `models/dqn` не изменены. Это фиксирует отрицательный
результат без выборочной публикации только удачных метрик.
