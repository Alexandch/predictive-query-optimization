# Domain-balanced validation DQN generic-v3

## Протокол

Для DQN выполнен отдельный подбор доли Pagila. Результат XGBoost не переносился
на DQN автоматически. До выбора финального кандидата запечатанные контроли не
читались.

Для каждого seed 21, 42 и 84 валидатор:

1. откладывает 20% структурных шаблонов отдельно в aviation, retail и Pagila;
2. сохраняет целиком все действия одного `query_id`;
3. формирует вложенные выборки Pagila;
4. обучает `generic-v3` только на оставшихся шаблонах;
5. считает accuracy и regret отдельно на трёх development-holdout;
6. выбирает минимальный macro mean regret, затем худший regret, accuracy и
   меньшую долю.

## Подбор доли

| Доля Pagila | Macro accuracy | Macro regret | Худший regret |
|---:|---:|---:|---:|
| 0% | 40,92% | 0,1008 | 0,1130 |
| 10% | **46,13%** | 0,1014 | 0,1161 |
| 25% | 42,96% | 0,0970 | 0,1109 |
| 50% | 43,38% | 0,0973 | 0,1126 |
| 75% | 44,06% | 0,0996 | 0,1110 |
| **100%** | 42,23% | **0,0920** | **0,1040** |

Уточняющая сетка 80–95% не улучшила regret. Номинальные 95% и 100% дали один
фактический набор из-за округления внутри небольших групп шаблонов. Выбран весь
доступный Pagila-набор, поскольку regret отражает стоимость неправильного
индексного решения, а не только число точных совпадений.

Итоговый набор содержит 6 370 действий, 1 378 решений и 116 шаблонов, включая
1 020 действий и 179 решений Pagila. SHA-256:
`29673F1C1F7F8BEEC06D94B975F231F68401F1B3C744127983B80557ADAB6361`.

## Финальное обучение

| Модель | Accuracy | Mean regret | Reward MAE |
|---|---:|---:|---:|
| Основная generic-v2 | 56,10% | 0,0749 | 0,1500 |
| generic-v3 seed 21 | 55,24% | 0,0730 | 0,1309 |
| generic-v3 seed 42 | 58,04% | 0,0728 | **0,0913** |
| **generic-v3 seed 84** | **69,58%** | **0,0570** | 0,1345 |

Seed 84 улучшил обе основные внутренние метрики и был единственным вариантом,
допущенным к внешнему шлюзу.

## Единственный внешний прогон

| Контроль | Основная accuracy / regret | Кандидат accuracy / regret | Решение |
|---|---:|---:|---|
| Aviation | **40,74%** / 0,0831 | 29,63% / **0,0788** | отклонён |
| Retail | **61,54% / 0,0607** | 50,00% / 0,0627 | отклонён |
| Logistics | **50,00% / 0,0603** | 46,43% / 0,1393 | отклонён |
| CH-benCHmark | 30,77% / 0,1118 | **34,62% / 0,0928** | прошёл |

Кандидат улучшил CH-benCHmark и regret aviation, но ухудшил accuracy трёх
остальных контролей и более чем удвоил regret logistics. Строгий шлюз не
пройден, поэтому основная DQN не заменена. Простое однократное индексное
действие достигло предела полезности текущего набора; следующий этап — среда с
последовательностью индексов, действием `STOP` и бюджетом хранения.

## Воспроизведение

```powershell
.\.venv\Scripts\pqo-merge-dqn.exe `
  artifacts\pagila\multidomain_pagila_only_dqn_v3.jsonl `
  dataset\postgresql\multidomain_dqn_augmented.jsonl `
  dataset\postgresql\pagila_dqn_training_v3.jsonl `
  --encoding-version generic-v3

.\.venv\Scripts\pqo-dqn-domain-mix.exe `
  artifacts\pagila\multidomain_pagila_only_dqn_v3.jsonl `
  artifacts\dqn-domain-mix `
  --fractions 0 0.1 0.25 0.5 0.75 1 `
  --seeds 21 42 84 --epochs 700

.\.venv\Scripts\pqo-dqn-train.exe `
  artifacts\dqn-domain-mix\selected_dqn_training.jsonl `
  artifacts\models\dqn_domain_mix_final_seed84 `
  --epochs 1200 --batch-size 64 --learning-rate 0.001 `
  --ranking-weight 0.10 --seed 84
```
