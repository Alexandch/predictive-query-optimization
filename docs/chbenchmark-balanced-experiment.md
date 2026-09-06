# Балансировка шаблонов CH-benCHmark

## Зачем проведён эксперимент

Простое добавление новой предметной области увеличило обучающий набор, но не
обеспечило переносимость на все независимые нагрузки. Причина в том, что шаблоны
с большим количеством уникальных параметризаций сильнее влияли на XGBoost и чаще
попадали в пакеты DQN.

В проект добавлены два воспроизводимых режима:

- `pqo-train --template-balanced` назначает каждому шаблону одинаковый суммарный
  вес, сохраняя все его реальные измерения;
- `pqo-dqn-train --sampling-mode template-balanced` равновероятно выбирает
  шаблоны, а затем одну полную группу действий запроса;
- `pqo-chbenchmark normalize-dqn` заменяет случайный выигрыш индекса штрафом за
  сложность только тогда, когда PostgreSQL не использовал временный индекс в
  плане. Исходное измерение сохраняется в `measured_reward`.

## Воспроизведение

```powershell
.\.venv\Scripts\pqo-chbenchmark.exe normalize-dqn `
  dataset\postgresql\chbenchmark_dqn_experience.jsonl `
  dataset\postgresql\chbenchmark_dqn_training.jsonl

.\.venv\Scripts\pqo-merge-dqn.exe artifacts\multidomain_ch_dqn_balanced.jsonl `
  dataset\postgresql\multidomain_dqn_augmented.jsonl `
  dataset\postgresql\chbenchmark_dqn_training.jsonl

.\.venv\Scripts\pqo-train.exe artifacts\chbenchmark\multidomain_ch_training.csv `
  artifacts\models\xgboost_balanced --seed 42 --target-transform log1p `
  --template-balanced

.\.venv\Scripts\pqo-dqn-train.exe artifacts\multidomain_ch_dqn_balanced.jsonl `
  artifacts\models\dqn_balanced --epochs 1200 --batch-size 64 `
  --learning-rate 0.001 --ranking-weight 0.10 --seed 21 `
  --sampling-mode template-balanced
```

Нормализация изменила 34 из 300 CH-записей. Итоговый DQN-набор содержит 5 650
действий. Для XGBoost использован ранее собранный объединённый набор: 14 560
измерений, 6 716 уникальных SQL.

SHA-256 `chbenchmark_dqn_training.jsonl`:
`51767241CCEEB8BCDF36F678F31804F8249C336383C33426940A9AAF32C2F483`.

## Результаты XGBoost

Внутренний parameter holdout: `R² = 0,9799`, `MAE = 13,90 мс`.
Внутренний unseen-template stress: `R² = 0,8868`, `MAE = 36,41 мс`.

| Контроль | Основная R² / MAE | Сбалансированная R² / MAE |
|---|---:|---:|
| aviation production-like | 0,442 / 602,60 мс | **0,536 / 533,31 мс** |
| retail | −0,460 / 307,24 мс | **0,335 / 192,16 мс** |
| logistics zero-shot | **0,864 / 114,85 мс** | 0,590 / 213,46 мс |
| CH unseen-template | −0,677 / 188,56 мс | **0,071 / 147,34 мс** |

Балансировка существенно улучшила три нагрузки, но ухудшила перенос на полностью
другую схему logistics. Проверка линейных смесей основной и сбалансированной
моделей с долей кандидата от 5% до 50% также не прошла строгий критерий: улучшение
трёх наборов сопровождалось ухудшением logistics.

## Результаты DQN

Внутренний test: accuracy `57,14%`, mean regret `0,0829`. Лучшая эпоха — 727,
ранняя остановка — 877.

| Контроль | Основная accuracy / regret | Сбалансированная accuracy / regret |
|---|---:|---:|
| aviation production-like | **40,74% / 0,0831** | 29,63% / 0,1004 |
| retail | **61,54% / 0,0607** | 57,69% / 0,0747 |
| logistics zero-shot | **50,00% / 0,0603** | 32,14% / 0,1036 |
| CH unseen-template | 30,77% / 0,1118 | **34,62% / 0,1069** |

## Решение

Оба кандидата отклонены строгим правилом «не хуже ни на одном контроле».
Основные модели в `models/xgboost` и `models/dqn` не заменены. Балансировка остаётся
доступной для дальнейших исследований и предметно-специализированных моделей.

CH-контроль уже использовался при выборе подхода и далее считается
development-control. Перед будущим продвижением модели следует один раз применить
новый структурно непересекающийся финальный набор, который не использовался для
подбора архитектуры или гиперпараметров.
