# Самостоятельное обучение и проверка моделей

## 1. Подготовка окружения

Команды выполняются в PowerShell из корня проекта:

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:PQO_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest
```

После повторного открытия PowerShell переменную для интеграционных тестов надо
задать снова. Для обычного прогноза она не требуется.

Полную проверку тестов и обеих готовых моделей можно запускать одной командой:

```powershell
.\scripts\verify.ps1
```

Учётные данные в `.env.example` предназначены только для локального Docker.
Для внешней или общей БД обязательно задайте другой пароль через переменные
окружения.

## 2. Повторное обучение XGBoost

Собрать стандартные запросы и сохранить измерения в служебной схеме БД:

```powershell
.\.venv\Scripts\pqo-generate.exe 2400 artifacts\xgb_run.csv --seed 42 --repetitions 2
```

Для нового запуска используйте другой seed. Дозапись в существующий CSV:

```powershell
.\.venv\Scripts\pqo-generate.exe 2400 artifacts\xgb_run.csv --seed 43 --repetitions 2 --append
```

Для собственных сложных запросов скопируйте
`dataset/query_manifest.example.csv`, задайте каждому структурному классу
стабильный `template_id`, изменяйте параметры SQL и выполните:

```powershell
.\.venv\Scripts\pqo-collect.exe dataset\query_manifest.example.csv artifacts\complex.csv
```

Так как все измерения сохраняются в PostgreSQL, объединённый набор удобнее
экспортировать из истории:

```powershell
.\.venv\Scripts\pqo-export.exe artifacts\xgb_all.csv --limit 10000
```

Либо объедините несколько совместимых CSV явно:

```powershell
.\.venv\Scripts\pqo-merge.exe artifacts\xgb_all.csv dataset\postgresql\aviation_dataset.csv artifacts\new_measurements.csv
```

Обучение с подбором гиперпараметров:

```powershell
.\.venv\Scripts\pqo-train.exe artifacts\xgb_all.csv artifacts\models\xgb_candidate --seed 42
```

Проверка произвольного запроса не выполняет сам `SELECT`, а получает только
оценочный план:

```powershell
.\.venv\Scripts\pqo-predict.exe artifacts\models\xgb_candidate\xgboost_query_time.joblib "SELECT * FROM aviation.flights WHERE departure_airport = 'MSQ'"
```

Смотрите `metrics.json`. Основные поля — `parameter_holdout.r2` и
`parameter_holdout.mae_ms`; устойчивость к новым структурам показывает
`unseen_template_stress.r2`.

## 3. Сколько ещё данных нужно XGBoost

Текущие 4 800 измерений покрывают 40 шаблонов. Универсальная модель получает
`R² = 0,9760`, а stress-R² новых структур — `0,8823` вместо прежних `0,7731`.
Поэтому простое повторение тех же запросов почти исчерпало пользу.

Следующая разумная цель:

- 48–60 структурных шаблонов вместо текущих 40;
- 100–200 уникальных наборов параметров на шаблон;
- 2–3 повтора каждого точного SQL для медианной агрегации;
- ориентировочно 10 000–20 000 сырых измерений в одной непрерывной серии.

Добавляйте оконные функции, CTE, несколько уровней подзапросов, 3–5 JOIN,
разные селективности, сортировки с LIMIT, HAVING, EXISTS/NOT EXISTS и запросы,
которые возвращают как очень мало, так и много строк. Не следует искусственно
делать SQL длинным: важнее разнообразие планов выполнения.

Остановить расширение можно, когда два последовательных увеличения набора на
20–25% дают прирост `unseen_template_stress.r2` менее `0,01`, MAE почти не
снижается, а целевой стресс-R² достиг хотя бы `0,90`. Контрольный набор и seed
при сравнении версий должны оставаться одинаковыми.

## 4. Обучение DQN

Сбор опыта выполняет реальные запросы и транзакционно создаёт индексы, поэтому
его следует запускать только на учебной копии БД:

```powershell
.\.venv\Scripts\pqo-dqn-collect.exe 480 artifacts\dqn.jsonl --actions-per-query 3 --repetitions 2 --seed 2027 --resume
```

`--resume` продолжает незавершённый запуск. Для более надёжного итогового
эксперимента используйте 1 000–2 000 запросов, 3–5 действий и 3 повтора. Это
обычно даст 4 000–10 000 записей опыта; время определяется размером таблиц и
скоростью создания индексов.

Обучение и основной parameter-holdout:

```powershell
.\.venv\Scripts\pqo-dqn-train.exe artifacts\dqn.jsonl artifacts\models\dqn --epochs 2000 --batch-size 128 --learning-rate 0.0005 --seed 42
```

Отдельный стресс-тест новых структур:

```powershell
.\.venv\Scripts\pqo-dqn-train.exe artifacts\dqn.jsonl artifacts\models\dqn_stress --epochs 2000 --batch-size 128 --learning-rate 0.001 --seed 42 --split-mode unseen-template
```

Получить рекомендацию без создания постоянного индекса:

```powershell
.\.venv\Scripts\pqo-recommend.exe artifacts\models\dqn\dqn_index_advisor.pt "SELECT flight_id FROM aviation.flights WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure"
```

Единый прикладной вызов XGBoost + DQN, используемый будущим PyQt-интерфейсом:

```powershell
.\.venv\Scripts\pqo-analyze.exe models\xgboost\xgboost_query_time.joblib models\dqn\dqn_index_advisor.pt "SELECT flight_id FROM aviation.flights WHERE departure_airport = 'MSQ' ORDER BY scheduled_departure" --threshold-ms 50
```

Без `--no-persist` прогноз и рекомендация записываются в служебные таблицы
`pqo.query_run`, `pqo.model_prediction` и `pqo.index_recommendation`.

Проверить рекомендацию реальным транзакционным экспериментом можно через
`pqo-index`: сначала выведите кандидатов, затем передайте номер действия.

```powershell
.\.venv\Scripts\pqo-index.exe "SELECT flight_id FROM aviation.flights WHERE departure_airport = 'MSQ'"
.\.venv\Scripts\pqo-index.exe --action-index 1 --repetitions 3 "SELECT flight_id FROM aviation.flights WHERE departure_airport = 'MSQ'"
```

В метриках DQN сравнивайте `recommendation_accuracy` и `mean_regret` с
`random_accuracy`, `random_mean_regret`, `noop_accuracy` и
`noop_mean_regret`. Высокая accuracy полезна, но низкий regret важнее: он
показывает, сколько ускорения теряется из-за выбранного действия.
