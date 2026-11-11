# Эксперименты и командная строка

Экспериментальные инструменты сохраняют отчёты и кандидатов отдельно от рабочих
весов. Результат конкретного запуска находится в его JSON/CSV и manifest, где
фиксируются данные, seed, параметры и отпечатки файлов. История изменений
доступна в Git; текущие инструкции не дублируют протокол каждого запуска.

## Основные команды

Все перечисленные команды установлены через `pip install -e ".[dev]"`.
Параметры и подкоманды доступны через `--help`.

| Задача | Команды |
| --- | --- |
| Сбор, экспорт и объединение времён | `pqo-collect`, `pqo-generate`, `pqo-export`, `pqo-merge` |
| Обучение и прогноз времени | `pqo-train`, `pqo-predict`, `pqo-calibrate` |
| Общий анализ, структурные гипотезы | `pqo-analyze`, `pqo-structure` |
| Пробные индексные действия | `pqo-index`, `pqo-recommend` |
| Сбор и обучение одиночной DQN | `pqo-dqn-collect`, `pqo-dqn-negative`, `pqo-dqn-sample`, `pqo-merge-dqn`, `pqo-dqn-train` |
| Последовательные индексные действия | `pqo-sequential-collect`, `pqo-sequential-merge`, `pqo-sequential-train`, `pqo-sequential-control` |
| Переписывание и журнал | `pqo-rewrite`, `pqo-rewrite-journal`, `pqo-rewrite-batch` |
| Офлайн-оценка переписываний | `pqo-rewrite-learning`, `pqo-rewrite-ranking`, `pqo-rewrite-pairwise` |
| Выбор направления оптимизации | `pqo-strategy` |
| Доменные нагрузки | `pqo-retail`, `pqo-pagila`, `pqo-chbenchmark` |
| Контроль и разбиения | `pqo-production-benchmark`, `pqo-retail-control`, `pqo-logistics-control`, `pqo-domain-mix`, `pqo-dqn-domain-mix`, `pqo-baseline`, `pqo-final-experiment` |

Точки входа определены в [pyproject.toml](../pyproject.toml).
CLI использует переменные окружения, независимо от сохранённых профилей GUI.

## Проверка оптимизаций

Повторяемый контроль индексных и SQL-вариантов:

```powershell
.\.venv\Scripts\python.exe -m pqo.optimization_control --output artifacts\optimization-control --timeout-ms 8000 --repetitions 3
.\.venv\Scripts\python.exe scripts\check_shared_aggregates.py --example examples\aviation-composite-distinct-rewrite.sql --output artifacts\shared-aggregates
```

Они реально выполняют SQL; индексные проверки создают пробные объекты с откатом.
Отсутствие достаточного выигрыша — допустимый результат, а не повод менять
пороги после просмотра. Примеры зависят от подготовленных схем и данных.

Для транзакций используйте [отдельную приёмку](testing.md#сквозная-проверка-транзакций).
Протокол самостоятельных SELECT не измеряет DML и не заменяет всю транзакцию.

## Сбор пар переписывания

`pqo-rewrite-batch` строит ограниченный набор по каталогам разрешённых схем.
Без `--execute` формируется предварительный набор; с ним запускаются сравнение
результатов и измерения:

```powershell
.\.venv\Scripts\pqo-rewrite-batch.exe --output artifacts\rewrite-batch --schema retail --max-cases 28
.\.venv\Scripts\pqo-rewrite-batch.exe --output artifacts\rewrite-batch-measured --schema retail --max-cases 28 --execute
```

Журнал хранит SQL и сырые измерения. При подготовке обучения `pqo-rewrite-learning`
аудирует их пригодность; development/control назначаются явным manifest.
Связанные формы исходного и переписанного SQL группируются вместе, включая
случай «вариант стал исходным». Неприсвоенные и встроенные smoke-записи не
становятся обучающими по умолчанию.

В признаки входят AST и оценочный план; фактические времена, actual-планы и
решение о принятии не используются как вход. Ранжирование и pairwise-модель
остаются офлайн: они не включены в рабочий генератор и не обходят проверку
эквивалентности. Подробности форматов доступны в `--help` соответствующих команд.

## Расширение моделей времени

Скрипты в [scripts](../scripts) разделяют этапы сбора, выбора и контроля:

- `renew_training_targets.py` — свежие времена вместо смешения старых целей;
- `heavy_fresh_experiment.py`, `ordered_features_experiment.py` — тяжёлые,
  оконные и упорядоченные агрегаты;
- `paired_regression_control.py` — парное сравнение замороженных моделей;
- `audit_first_prediction.py`, `audit_paired_targets.py`, `audit_user_control.py`
  — аудит первого прогноза, целей и пересечений с контролем;
- `window_cte_experiment.py`, `aggregation_development_experiment.py`,
  `complex_aggregation_experiment.py` — отдельные семейства SQL.

Сначала просмотрите `--help` и входные manifest: отдельные этапы требуют
результатов предыдущего сбора. Используйте новые каталоги запуска. Сохранённые
контрольные SQL нельзя включать в обучение для улучшения того же отчёта.
