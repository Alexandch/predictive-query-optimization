# Проверка leave-one-database-out

## Назначение

Эксперимент проверяет переносимость моделей на базу данных, которую они никогда
не видели при обучении. Основные XGBoost и DQN обучены только на схемах
`aviation` и `retail`. Третья схема `logistics`, её SQL и результаты выполнения
не входят в обучающие наборы и используются только как запечатанный zero-shot
контроль.

## Независимая база

Схема `logistics` содержит 10 связанных сущностей и 2 275 050 строк:

| Сущность | Строк |
|---|---:|
| `suppliers` | 5 000 |
| `facilities` | 30 |
| `items` | 20 000 |
| `purchase_orders` | 120 000 |
| `purchase_order_lines` | 480 000 |
| `carriers` | 20 |
| `shipments` | 150 000 |
| `shipment_items` | 450 000 |
| `tracking_events` | 750 000 |
| `stock_movements` | 300 000 |

Контрольная нагрузка состоит из 15 новых структур: OTIF поставщиков, перцентили
доставки, пропуски в tracking events, пропускная способность объектов, остатки,
сверка заказов, эффективность перевозчиков, маршруты, исключения, velocity
товаров, последнее событие, риск stockout, концентрация поставщиков, плотность
стоимости отправки и выбросы lead time.

## Результаты zero-shot

XGBoost оценён на 150 реальных выполнениях 58 уникальных SQL:

| Метрика | Значение |
|---|---:|
| R² | 0,8636 |
| MAE | 114,85 мс |
| Median AE | 40,05 мс |
| P90 AE | 406,83 мс |
| RMSE | 215,94 мс |
| MAPE | 33,08% |

DQN оценён на 28 запросах и 84 измеренных действиях:

| Стратегия | Accuracy | Mean regret |
|---|---:|---:|
| DQN `generic-v2`, до расширения | 39,29% | 0,1064 |
| DQN `generic-v2`, с отрицательными примерами | **50,00%** | **0,0603** |
| случайный выбор | 21,43% | 0,2557 |
| всегда `NOOP` | 14,29% | 0,3023 |

XGBoost сохранил высокий R² на полностью новой предметной области. Расширение
DQN отрицательными примерами из `aviation` и `retail` повысило accuracy на
логистике на 10,71 п.п. и снизило regret на 43,39%. Данные данного контроля при
обучении не использовались.

## Воспроизведение

```powershell
docker compose up -d --wait
$env:PQO_ALLOWED_SCHEMAS = "aviation,retail,logistics"

.\.venv\Scripts\pqo-logistics-control.exe collect-xgb 75 dataset\postgresql\logistics_zero_shot.csv --repetitions 2
.\.venv\Scripts\pqo-logistics-control.exe collect-dqn 30 dataset\postgresql\logistics_dqn_zero_shot.jsonl --actions-per-query 2
.\.venv\Scripts\pqo-logistics-control.exe evaluate dataset\postgresql\logistics_zero_shot.csv models\xgboost\xgboost_query_time.joblib artifacts\evaluation\zero_shot_logistics --dqn-experience dataset\postgresql\logistics_dqn_zero_shot.jsonl --dqn-model models\dqn\dqn_index_advisor.pt
```

SHA-256 контрольных данных:

- `logistics_zero_shot.csv`: `D9E54F56C95ADC9B77478C5863711C644218A73F128932D664ABD91A1A74CE2C`;
- `logistics_dqn_zero_shot.jsonl`: `DEAEA35BCBB5BBF7CFED5CDBBC17FB2BF3A2F92340F538C50EB6513FBE589197`.
