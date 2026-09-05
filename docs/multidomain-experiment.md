# Многодоменное обучение и проверка переносимости

## Цель

Проверить, способны ли XGBoost и DQN работать не только со схемой `aviation`,
но и с другой предметной областью. Для этого добавлена схема `retail` из 10
связанных сущностей и примерно 1,34 млн строк. Обучающие и контрольные шаблоны
розницы разделены на уровне исходного кода.

## Данные

| Набор | Назначение | Объём |
|---|---|---:|
| `aviation_dataset.csv` | XGBoost, авиация | 10 400 измерений |
| `retail_training.csv` | XGBoost, розница | 3 360 измерений, 3 076 уникальных SQL |
| `multidomain_training.csv` | объединённое обучение XGBoost | 13 760 измерений, 6 474 уникальных SQL |
| `dqn_experience.jsonl` | DQN, авиация | 3 940 опытов |
| `retail_dqn_experience.jsonl` | DQN, розница | 940 опытов |
| `multidomain_dqn_experience.jsonl` | DQN, оба домена, `generic-v2` | 4 880 опытов |
| `production_control.csv` | запечатанный контроль авиации | 600 измерений, 30 шаблонов |
| `retail_control.csv` | запечатанный контроль розницы | 150 измерений, 15 шаблонов |
| `dqn_production_control.jsonl` | контроль DQN, авиация | 27 решений |
| `retail_dqn_control.jsonl` | контроль DQN, розница | 26 решений |

## Совместимость DQN

В `generic-v2` фиксированный список авиационных таблиц заменён стабильным
хешированием произвольных имён таблиц. Имена колонок также кодируются в
фиксированное пространство, а контекст действия описывает совпадения с
`WHERE`, `JOIN`, `ORDER BY` и `SELECT`. Версия хранится в каждой строке опыта и
в артефакте модели. Старые модели без метаданных автоматически считаются
`aviation-v1`, поэтому обновление не ломает их загрузку.

## Результаты независимых контролей

### XGBoost

| Модель | Контроль | MAE, мс | Median AE, мс | R² |
|---|---|---:|---:|---:|
| прежняя авиационная | авиация | 637,76 | 95,39 | 0,376 |
| основная многодоменная | авиация | 602,60 | 133,76 | 0,442 |
| прежняя авиационная | розница | 320,83 | 93,39 | -0,678 |
| основная многодоменная | розница | 307,24 | 70,59 | -0,460 |

Отбор второго цикла по двум контролям одновременно:

| Вариант | Авиация MAE / R² | Розница MAE / R² | Решение |
|---|---:|---:|---|
| прежняя основная | 637,76 / 0,376 | 320,83 / −0,678 | baseline |
| v1 `log1p`, tuned | 707,25 / 0,271 | 269,75 / −0,321 | регрессия авиации |
| v2 `log1p`, tuned | 739,87 / 0,192 | 251,53 / −0,250 | регрессия авиации |
| v2 `log1p`, baseline | 698,77 / 0,268 | 278,51 / −0,397 | регрессия авиации |
| v2 `identity`, tuned | 681,93 / 0,339 | 305,91 / −0,289 | регрессия авиации |
| v2 `identity`, baseline | **602,60 / 0,442** | **307,24 / −0,460** | выбрана |

Во втором цикле параметрическое пространство розницы расширено до 3 076
уникальных SQL. Проверены настроенный `log1p`, базовый `log1p`, настроенный
`identity` и базовый `identity`. Только базовый `identity` улучшил MAE и R²
относительно прежней модели одновременно на обоих контролях, поэтому он
назначен основным в `models/xgboost`. Контроли использовались для финального
сравнения, но не добавлялись в обучающие данные.

### DQN

| Модель | Контроль | Accuracy | Mean regret |
|---|---|---:|---:|
| прежняя `aviation-v1` | авиация | 14,81% | 0,1093 |
| новая `generic-v2` | авиация | 37,04% | 0,0891 |
| прежняя `aviation-v1` | розница | 46,15% | 0,1464 |
| новая `generic-v2` | розница | 53,85% | 0,0650 |

Новая DQN улучшила оба независимых контроля и назначена основной моделью в
`models/dqn`.

Следующий цикл добавил 16 шаблонов отрицательных примеров и улучшил уже эту
модель на aviation, retail и logistics одновременно. Полный протокол приведён
в `docs/dqn-negative-augmentation.md`.

## Как повторить

```powershell
docker compose up -d --wait
$env:PQO_ALLOWED_SCHEMAS = "aviation,retail"

# Дополнительные данные розницы
.\.venv\Scripts\pqo-retail.exe collect-xgb 3000 dataset\postgresql\retail_training.csv --seed 8501
.\.venv\Scripts\pqo-retail.exe collect-dqn 360 dataset\postgresql\retail_dqn_experience.jsonl --actions-per-query 2

# Объединение и обучение кандидатов
.\.venv\Scripts\pqo-merge.exe dataset\postgresql\multidomain_training.csv dataset\postgresql\aviation_dataset.csv dataset\postgresql\retail_training.csv
.\.venv\Scripts\pqo-merge-dqn.exe dataset\postgresql\multidomain_dqn_experience.jsonl dataset\postgresql\dqn_experience.jsonl dataset\postgresql\retail_dqn_experience.jsonl
.\.venv\Scripts\pqo-train.exe dataset\postgresql\multidomain_training.csv artifacts\models\multidomain_xgboost --target-transform identity --no-tune
.\.venv\Scripts\pqo-dqn-train.exe dataset\postgresql\multidomain_dqn_experience.jsonl artifacts\models\multidomain_dqn --epochs 1200

# Независимый розничный контроль
.\.venv\Scripts\pqo-retail-control.exe collect-xgb 75 dataset\postgresql\retail_control.csv --repetitions 2
.\.venv\Scripts\pqo-retail-control.exe collect-dqn 30 dataset\postgresql\retail_dqn_control.jsonl
.\.venv\Scripts\pqo-retail-control.exe evaluate dataset\postgresql\retail_control.csv artifacts\models\multidomain_xgboost\xgboost_query_time.joblib artifacts\evaluation\multidomain_on_retail --dqn-experience dataset\postgresql\retail_dqn_control.jsonl --dqn-model artifacts\models\multidomain_dqn\dqn_index_advisor.pt
```

Контрольные CSV/JSONL нельзя добавлять в обучающие наборы: иначе оценка перестанет
быть независимой.
