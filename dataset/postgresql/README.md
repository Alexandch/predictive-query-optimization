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

## Leave-one-database-out: логистика

`logistics_zero_shot.csv` содержит 150 измерений, 58 уникальных SQL и 15 новых
шаблонов. `logistics_dqn_zero_shot.jsonl` содержит 84 действия для 28 решений.
Основные модели обучены только на `aviation` и `retail`; оба logistics-набора
запрещено объединять с обучающими данными.

- `logistics_zero_shot.csv`: `D9E54F56C95ADC9B77478C5863711C644218A73F128932D664ABD91A1A74CE2C`;
- `logistics_dqn_zero_shot.jsonl`: `DEAEA35BCBB5BBF7CFED5CDBBC17FB2BF3A2F92340F538C50EB6513FBE589197`.

## Отрицательные обучающие примеры DQN

`dqn_negative_experience.jsonl` содержит 470 записей, 92 уникальных запроса и
16 обучающих шаблонов `aviation`/`retail`. Для индекса, отсутствующего в плане,
измерительный шум заменён штрафом сложности; исходная награда сохранена в
`measured_reward`. `multidomain_dqn_augmented.jsonl` объединяет 5 350 записей и
86 шаблонов. Ни один контрольный набор в объединение не входит.

- `dqn_negative_experience.jsonl`: `35673C1A880799262CFE4033706AAB4D5E0F35FEDA223941CF288F08334F03BC`;
- `multidomain_dqn_augmented.jsonl`: `F1074CBBAFD92169FAC6D622CFA30C00EC8FEC7F9F4632622712B2DD1F009D96`.

Второй hard-negative набор `dqn_hard_negative_v2_experience.jsonl` содержит
600 действий, 62 уникальных SQL и ещё 16 обучающих шаблонов. Его SHA-256:
`DF8BFAA91B9936F085764B3A2BBA46046B1F3BD4E424DB13F178E4585351548E`.
Он сохранён для следующего цикла признаков, но кандидаты `generic-v2` с добавкой
25%, 50% и 100% отклонены внешним шлюзом; основная DQN не заменена.

## CH-benCHmark-совместимая предметная область

Схема `chbenchmark` содержит 12 сущностей заказов, складов и поставщиков.
Обучающие и контрольные структуры реализованы разными генераторами и не
пересекаются по `template_id`.

- `chbenchmark_training.csv`: 800 измерений, 242 уникальных SQL, 20 шаблонов;
- `chbenchmark_control.csv`: 200 измерений, 60 уникальных SQL, 10 шаблонов;
- `chbenchmark_dqn_experience.jsonl`: 300 действий, 86 решений, 20 шаблонов;
- `chbenchmark_dqn_control.jsonl`: 90 действий, 26 решений, 10 шаблонов.

SHA-256:

- `chbenchmark_training.csv`: `1700ED6B21D1C2FFD753F92931AED5FC357375F1312284589B57F644610BA418`;
- `chbenchmark_control.csv`: `187EE469FC7252C3508FBA0BCECBDBF3335FB90F101EE30B9D2DE040A433A1B2`;
- `chbenchmark_dqn_experience.jsonl`: `22ECCEE78306BB3C7C9913C21F5805E3B4E50F520E8ABF4E4686CBB18F9CF9EF`;
- `chbenchmark_dqn_control.jsonl`: `0A16DD5BE0191EEAA49F83C2CD149F5FF3EF31C6A23132597A5AED31910D5DD2`.

Контрольные `chbenchmark_control.csv` и `chbenchmark_dqn_control.jsonl`
запрещено объединять с обучающими данными. Протокол и результаты приведены в
`docs/chbenchmark-experiment.md`.

## Pagila development

`pagila_training.csv` содержит 1 200 измерений, 295 уникальных SQL и 30 новых
шаблонов. `pagila_dqn_experience_v3.jsonl` содержит 1 020 действий для 179
решений; нормализованная копия находится в `pagila_dqn_training_v3.jsonl`.

- `pagila_training.csv`: `948B6BDB47716632090DBA51E7B721B0701F9119AE7A81C2129EA23320BF7829`;
- `pagila_dqn_experience_v3.jsonl`: `2E518745A48EEB3571397889707AA55259FF1F3D80974DF633061FB67A118EF4`;
- `pagila_dqn_training_v3.jsonl`: `EB923CBF7A3E298596E12A6B8A86D9872353DEF9FAC7E94386475AD1EABF120B`.

Это development-набор, а не новый запечатанный контроль. Первый цикл
кандидатов отклонён; основные модели не перезаписаны.

