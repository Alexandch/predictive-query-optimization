# Последовательная DQN

Артефакт `sequential_dqn_index_advisor.pt` использует кодирование
`generic-v4-sequential`, Bellman discount `0,95` и validation-порог CREATE
`0,387249`. SHA-256:
`4CE5EDC815F5BDEDEE62DC704AC4C9201C44B8F9F684B904E241F5CC34343B1A`.

Модель предназначена для измеренного глубокого анализа. Каждый пробный индекс
создаётся в откатываемой транзакции; приложение не применяет показанный DDL.
Результаты независимого контроля описаны в
`docs/sequential-control-benchmark.md`.
