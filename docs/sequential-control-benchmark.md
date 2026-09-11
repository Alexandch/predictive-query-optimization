# Независимый контроль последовательной DQN

## Протокол

До открытия контролей были зафиксированы модель с SHA-256
`4CE5EDC815F5BDEDEE62DC704AC4C9201C44B8F9F684B904E241F5CC34343B1A`,
`gamma = 0,95` и порог CREATE `0,387249`. Порог, веса и эпоха после просмотра
результатов не изменялись. В обучение не включались запросы или награды схем
`logistics` и `chbenchmark`.

```powershell
.\.venv\Scripts\pqo-sequential-control.exe collect logistics 100 `
  artifacts\sequential-control\logistics.jsonl --seed 10401

.\.venv\Scripts\pqo-sequential-control.exe collect chbenchmark 100 `
  artifacts\sequential-control\chbenchmark.jsonl --seed 16101

.\.venv\Scripts\pqo-sequential-control.exe evaluate `
  artifacts\sequential-control\logistics.jsonl `
  artifacts\models\sequential_dqn_ranked_calibrated\sequential_dqn_index_advisor.pt `
  artifacts\sequential-control\logistics-report --seed 742
```

Для CH выполняется та же команда `evaluate` с CH-набором и seed `743`.
Созданные индексы находятся внутри откатываемых транзакций. После двух сборов
число индексов `pqo_seq_%` в PostgreSQL равно нулю.

## Результаты

| Контроль | Политика | Accuracy | Mean regret |
|---|---|---:|---:|
| logistics, 155 состояний | sequential DQN | **62,58%** | 0,1045 |
| | всегда STOP | 54,19% | 0,1731 |
| | random | 46,45% | **0,0990** |
| CH, 152 состояния | sequential DQN | 75,00% | **0,0247** |
| | всегда STOP | **75,66%** | 0,0258 |
| | random | 43,42% | 0,0317 |
| оба контроля, 307 состояний | sequential DQN | **68,73%** | **0,0650** |
| | всегда STOP | 64,82% | 0,1002 |
| | random | 44,95% | 0,0657 |

В парном bootstrap по состояниям (20 000 повторов, seed 803) изменение DQN
относительно STOP составило `+3,91` п.п. accuracy, 95% ДИ
`[+0,65; +7,17]`, и `−0,0352` regret, 95% ДИ `[−0,0526; −0,0196]`.
На CH разница accuracy равна одному состоянию и статистически неотличима от
нуля: 95% ДИ `[−3,29; +1,32]` п.п.

Старые результаты однократной DQN нельзя считать прямой контрольной группой:
у неё другое кодирование, `NOOP` вместо `STOP`, иное число измеренных действий
и нет Bellman-возврата. Справочно её историческая accuracy равна 50,00% на
logistics и 30,77% на CH, тогда как последовательная модель получила 62,58% и
75,00% соответственно.

## Решение и ограничение

Кандидат прошёл офлайн-контроль относительно безопасной STOP-политики в
объединённой выборке. Однако в каждом состоянии контроль содержит STOP и один
случайно измеренный CREATE. Он проверяет переносимость Q-функции и Bellman-
цепочки, но не воспроизводит окончательный выбор среди всех доступных индексов.

Поэтому модель допускается к следующему этапу — end-to-end rollout полного
списка действий — но пока не заменяет основную однократную DQN в приложении.
