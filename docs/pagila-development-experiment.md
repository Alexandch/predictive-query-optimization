# Development-эксперимент Pagila и generic-v3

## База и воспроизводимость

В качестве новой предметной области используется PostgreSQL sample database
Pagila v3.1.0, commit
`fef9675714cfba1756df4719b5e36075a7ddf90e`. Исходная лицензия и точный
provenance находятся в `database/vendor/pagila`. Адаптированные schema/data SQL
автоматически загружаются Docker Compose при создании нового тома.

Схема `pagila` содержит 15 основных таблиц и семь секций таблицы платежей. В
данных находятся 16 044 проката и 16 049 платежей. Существующие схемы и
запечатанные контроли не изменялись.

## Нагрузка

Собственный генератор проекта создаёт 30 структурных шаблонов: оконные функции,
CTE, anti-join, коррелированные подзапросы, `GROUPING SETS`, percentile,
агрегации с `FILTER`, self-join и многотабличные отчёты.

- `pagila_training.csv`: 1 200 измерений, 295 уникальных SQL, 30 шаблонов;
- `pagila_dqn_experience_v3.jsonl`: 1 020 сырых действий, 179 решений;
- `pagila_dqn_training_v3.jsonl`: нормализованная обучающая копия;
- `NO-OP` лучший в 68,72% решений;
- 84,03% индексных действий в обучающей копии отрицательны.

SHA-256:

- XGBoost: `948B6BDB47716632090DBA51E7B721B0701F9119AE7A81C2129EA23320BF7829`;
- DQN raw: `2E518745A48EEB3571397889707AA55259FF1F3D80974DF633061FB67A118EF4`;
- DQN normalized: `EB923CBF7A3E298596E12A6B8A86D9872353DEF9FAC7E94386475AD1EABF120B`.

## Generic-v3

Версия `generic-v3` сохраняет все признаки `generic-v2` и добавляет:

- число строк и физический размер именно целевой таблицы;
- количество её существующих индексов;
- наличие точного или префиксного индекса;
- число ключевых колонок под функцией или выражением;
- признак несаргируемости первой ключевой колонки.

Контекст сохраняется внутри каждой experience-записи. Основная модель
`generic-v2` продолжает поддерживаться без изменения формата.

## XGBoost

Объединённый набор содержит 14 960 измерений. Проверены обычное обучение и
равный вес структурных шаблонов.

| Модель | Parameter R² / MAE | Unseen-template R² / MAE |
|---|---:|---:|
| Основная | 0,9701 / 14,18 мс | 0,7761 / 47,75 мс |
| Pagila regular | 0,9777 / 13,00 мс | 0,7108 / 57,73 мс |
| Pagila balanced | **0,9796 / 12,54 мс** | **0,8792 / 41,17 мс** |

Несмотря на сильные внутренние метрики, balanced-кандидат ухудшил все три
старых домена. Regular-кандидат улучшил MAE aviation/retail и обе метрики CH,
но немного снизил R² aviation/retail/logistics. По строгому шлюзу оба кандидата
отклонены.

| Контроль | Основная R² / MAE | Regular | Balanced |
|---|---:|---:|---:|
| Aviation | **0,4417** / 602,60 | 0,4265 / **600,21** | 0,3602 / 674,17 |
| Retail | **−0,4597** / 307,24 | −0,4847 / **298,11** | −0,9823 / 351,89 |
| Logistics | **0,8636 / 114,85** | 0,8523 / 114,86 | 0,7547 / 152,94 |
| CH-benCHmark | −0,6772 / 188,56 | **−0,3736 / 164,64** | −0,3959 / 194,53 |

## DQN

Проверены два состава: с hard-negative-v2 (6 970 действий) и без него
(6 370 действий), каждый с seed 21, 42 и 84. Лучшие внутренние варианты дали
accuracy до 69,58% и regret 0,0570, но ни один не улучшил одновременно четыре
внешних контроля. Наиболее показательный `Pagila-only / seed 21` улучшил
aviation до 48,15% / 0,0487 и CH accuracy до 38,46%, но ухудшил retail и
logistics. Все DQN-кандидаты отклонены.

Основные XGBoost и DQN не заменялись. Последующий development-only подбор доли
описан в `domain-balanced-validation.md`: лучшей оказалась примесь 10%, но
финальный XGBoost-кандидат не прошёл внутренний unseen-template шлюз.

## Воспроизведение

```powershell
.\.venv\Scripts\pqo-pagila.exe collect-xgb 600 dataset\postgresql\pagila_training.csv --seed 18101 --repetitions 2
.\.venv\Scripts\pqo-pagila.exe collect-dqn 300 dataset\postgresql\pagila_dqn_experience_v3.jsonl --actions-per-query 3 --repetitions 2 --seed 18101
.\.venv\Scripts\pqo-pagila.exe normalize-dqn dataset\postgresql\pagila_dqn_experience_v3.jsonl dataset\postgresql\pagila_dqn_training_v3.jsonl
.\.venv\Scripts\pqo-merge-dqn.exe artifacts\pagila\multidomain_pagila_only_dqn_v3.jsonl dataset\postgresql\multidomain_dqn_augmented.jsonl dataset\postgresql\pagila_dqn_training_v3.jsonl --encoding-version generic-v3
```
