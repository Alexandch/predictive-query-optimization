# Production-like control results

Каталог содержит отчёты замороженных XGBoost и DQN на 30 новых production-like
структурах. Эти данные не участвовали в обучении.

- `xgboost_control_metrics.json` — общие метрики;
- `xgboost_control_predictions.csv` — факт, прогноз и ошибка каждого SQL;
- `xgboost_control_by_template.csv` — ошибки по структурным классам;
- `dqn_control_metrics.json` — DQN против Random и NOOP;
- `dqn_control_decisions.csv` — выбранное, фактически лучшее действие и regret.
- `manifest.json` — условия прогона и SHA-256 выборок/моделей.

Полная методика описана в `docs/production-control-benchmark.md`.

Каталог `negative_augmented` содержит итоговые DQN-метрики после расширения
отрицательными примерами для aviation, retail и logistics. Методика и сравнение
описаны в `docs/dqn-negative-augmentation.md`.

Каталог `chbenchmark` содержит сравнение выбранных моделей и отклонённых
кандидатов на отдельной гибридной схеме заказов. Методика описана в
`docs/chbenchmark-experiment.md`.
