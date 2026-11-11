# Измерения PostgreSQL

CSV хранят исходный SQL, `template_id`, признаки AST/оценочного плана/каталога
и фактическую длительность в миллисекундах. JSONL индексного опыта содержит
состояние, типизированное действие и награду реального эксперимента с откатом.
Измеренные значения Actual не используются как доступные до выполнения признаки.

## Обучающие наборы

| Файлы | Назначение |
| --- | --- |
| `aviation_dataset.csv`, `dqn_experience.jsonl` | базовые измерения и индексный опыт авиационной схемы |
| `retail_training.csv`, `retail_dqn_experience.jsonl` | обучение на розничной схеме |
| `multidomain_training.csv`, `multidomain_dqn_experience.jsonl` | объединённые aviation/retail |
| `dqn_negative_experience.jsonl`, `multidomain_dqn_augmented.jsonl` | отрицательные индексные примеры и объединённый набор |
| `dqn_hard_negative_v2_experience.jsonl` | дополнительный hard-negative опыт для отдельных кандидатов |
| `chbenchmark_training.csv`, `chbenchmark_dqn_experience.jsonl`, `chbenchmark_dqn_training.jsonl` | обучающие структуры CH-benCHmark, сырой и нормализованный опыт |
| `pagila_training.csv`, `pagila_dqn_experience_v3.jsonl`, `pagila_dqn_training_v3.jsonl` | development-нагрузка Pagila, сырой и нормализованный опыт |
| `strategy_workload.jsonl`, `strategy_experience.jsonl` | нагрузка и опыт выбора направления оптимизации |

Нормализованный reward хранится отдельно от исходного `measured_reward`:
шумовое улучшение неиспользованного индекса не становится полезным действием.
Наличие hard-negative данных не означает, что обученный на них кандидат был принят.

## Контрольные наборы

| Файлы | Роль |
| --- | --- |
| `production_control.csv`, `dqn_production_control.jsonl` | отложенные production-like структуры |
| `retail_control.csv`, `retail_dqn_control.jsonl` | отдельные структуры retail |
| `logistics_zero_shot.csv`, `logistics_dqn_zero_shot.jsonl` | исходный leave-one-database-out контроль logistics |
| `chbenchmark_control.csv`, `chbenchmark_dqn_control.jsonl` | отдельные контрольные структуры CH-benCHmark |

Эти файлы не следует объединять с обучением для оценки на том же контроле.
После выбора по просмотренным результатам набор уже не является новым независимым
испытанием. Pagila — development-домен, не запечатанный внешний контроль.

## Воспроизведение

Базовый CSV собирается `pqo-generate`, собственные семейства — `pqo-collect` по
[манифесту](../query_manifest.example.csv). Для доменов и индексного опыта есть
отдельные CLI из [experiments.md](../../docs/experiments.md).
Инструкции по обучению — в [training-guide.md](../../docs/training-guide.md).

У разных поколений CSV различаются контракты признаков. Не добавляйте отсутствующие
поля нулями и не смешивайте старые/новые времена без проверки условий измерения.
Отпечатки данных и весов для конкретного контроля находятся в manifest рядом
с его результатами в [models](../../models).
