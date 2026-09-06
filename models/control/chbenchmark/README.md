# CH-benCHmark-derived external control

`primary` contains the selected XGBoost/DQN results before CH training.
`candidate` contains the first models trained with the CH training additions.
Both directories were evaluated on the same isolated CH control workload.

The candidate artifacts were rejected and are intentionally not copied into
`models/xgboost` or `models/dqn`. Full methodology and commands are documented
in `docs/chbenchmark-experiment.md`.
