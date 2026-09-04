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
| `retail_training.csv` | XGBoost, розница | 360 измерений, 154 уникальных SQL |
| `multidomain_training.csv` | объединённое обучение XGBoost | 10 760 измерений |
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
| многодоменный кандидат | авиация | 707,25 | 130,44 | 0,271 |
| прежняя авиационная | розница | 320,83 | 93,39 | -0,678 |
| многодоменный кандидат | розница | 269,75 | 75,11 | -0,321 |

Кандидат лучше переносится на розницу, но ухудшает авиационный контроль.
Поэтому он сохранён в `models/candidates/multidomain_xgboost`, но не назначен
основным. Следующий цикл должен увеличить долю розничных параметризаций и
оптимизировать многозадачный критерий по обоим контролям.

### DQN

| Модель | Контроль | Accuracy | Mean regret |
|---|---|---:|---:|
| прежняя `aviation-v1` | авиация | 14,81% | 0,1093 |
| новая `generic-v2` | авиация | 37,04% | 0,0891 |
| прежняя `aviation-v1` | розница | 46,15% | 0,1464 |
| новая `generic-v2` | розница | 53,85% | 0,0650 |

Новая DQN улучшила оба независимых контроля и назначена основной моделью в
`models/dqn`.

## Как повторить

```powershell
docker compose up -d --wait
$env:PQO_ALLOWED_SCHEMAS = "aviation,retail"

# Дополнительные данные розницы
.\.venv\Scripts\pqo-retail.exe collect-xgb 180 dataset\postgresql\retail_training.csv --repetitions 2
.\.venv\Scripts\pqo-retail.exe collect-dqn 360 dataset\postgresql\retail_dqn_experience.jsonl --actions-per-query 2

# Объединение и обучение кандидатов
.\.venv\Scripts\pqo-merge.exe dataset\postgresql\multidomain_training.csv dataset\postgresql\aviation_dataset.csv dataset\postgresql\retail_training.csv
.\.venv\Scripts\pqo-merge-dqn.exe dataset\postgresql\multidomain_dqn_experience.jsonl dataset\postgresql\dqn_experience.jsonl dataset\postgresql\retail_dqn_experience.jsonl
.\.venv\Scripts\pqo-train.exe dataset\postgresql\multidomain_training.csv artifacts\models\multidomain_xgboost
.\.venv\Scripts\pqo-dqn-train.exe dataset\postgresql\multidomain_dqn_experience.jsonl artifacts\models\multidomain_dqn --epochs 1200

# Независимый розничный контроль
.\.venv\Scripts\pqo-retail-control.exe collect-xgb 75 dataset\postgresql\retail_control.csv --repetitions 2
.\.venv\Scripts\pqo-retail-control.exe collect-dqn 30 dataset\postgresql\retail_dqn_control.jsonl
.\.venv\Scripts\pqo-retail-control.exe evaluate dataset\postgresql\retail_control.csv artifacts\models\multidomain_xgboost\xgboost_query_time.joblib artifacts\evaluation\multidomain_on_retail --dqn-experience dataset\postgresql\retail_dqn_control.jsonl --dqn-model artifacts\models\multidomain_dqn\dqn_index_advisor.pt
```

Контрольные CSV/JSONL нельзя добавлять в обучающие наборы: иначе оценка перестанет
быть независимой.
