# PostgreSQL aviation dataset

`aviation_dataset.csv` содержит 10 400 реальных запусков 52 структурных шаблонов
и 3 398 уникальных SQL на восьми сущностях схемы `aviation`.

- SHA-256: `70148556DA219CCFF6E6EFD6E2AF18668EED56C0298C825811E4DF0B9B6A3068`;
- единица целевой переменной: миллисекунды;
- исходный SQL и идентификатор шаблона сохранены;
- признаки SQL получены из AST;
- признаки плана получены из PostgreSQL JSON EXPLAIN;
- размеры, число строк и индексы отношений получены из каталога PostgreSQL;
- целевая переменная получена через `EXPLAIN ANALYZE`.

Набор воспроизводится командой `pqo-generate`; из сохранённой истории БД его
можно перестроить командой `pqo-export`.

`dqn_experience.jsonl` содержит 3 940 записей опыта для 826 уникальных запросов
52 структурных типов. SHA-256:
`C343F301825FC7DA7FEBA3BA3B742D9B74ABE162DA46D97F24197B4208435278`.
Каждая строка хранит состояние, типизированное действие и награду реального
транзакционного эксперимента. Постоянные индексы при сборе не создаются.

`production_control.csv` — независимый holdout из 600 измерений, 171 уникального SQL
и 30 production-like структур. SHA-256:
`7C2D9E8EE72955462CFB1812E6E7F9AEDD9B4639EA12BAEFAE1F1F12851C65C3`.

`dqn_production_control.jsonl` хранит 84 контрольных индексных действия.
SHA-256: `BC2A5A79E65DFE88C76D6619DCE948008060AA703CAAAF84B684C9ABC35A0870`.
Оба контрольных набора изолированы от обучения.

## Розничная предметная область

`retail_training.csv` содержит 3 360 измерений, 3 076 уникальных SQL и 18 обучающих шаблонов, а
`retail_dqn_experience.jsonl` — 940 записей реального опыта. Объединённые файлы
`multidomain_training.csv` и `multidomain_dqn_experience.jsonl` содержат
соответственно 13 760 измерений и 4 880 действий. Весь объединённый DQN-набор
использует универсальное кодирование `generic-v2`.

`retail_control.csv` (150 измерений, 15 новых шаблонов) и
`retail_dqn_control.jsonl` (80 действий для 26 решений) являются независимым
контролем и не должны использоваться для обучения.

SHA-256 новых наборов:

- `retail_training.csv`: `A0A7C19A121E71A58B37F18EDD0D290BE436862794EE0DCDCF032CD8C3A28F6E`;
- `retail_dqn_experience.jsonl`: `8F8CE7572857BF8DF3811CC2972D4A846C5B45A57AC7457651486216C0F449E8`;
- `multidomain_training.csv`: `E581A19D676D1119835C1496AC164C71ED14C49692FE534F12A69C4F91C4337D`;
- `multidomain_dqn_experience.jsonl`: `9F4BAE370FE790932C97BCB3106FDB68879D7F5A8B2C4C1710D0200E9934F4EA`;
- `retail_control.csv`: `5EEF579F25156B62CB21AB0258631CF744F82FF430B34E9ABAFB9EFDD613AF8F`;
- `retail_dqn_control.jsonl`: `0946D18BF120B84D028312169864E68BB797809B3CBB5A081F10CF45F3A561A3`.

