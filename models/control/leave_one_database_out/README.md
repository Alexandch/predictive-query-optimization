# Исходный leave-one-database-out контроль

Каталог `logistics` фиксирует первое zero-shot измерение DQN до расширения
отрицательными примерами: accuracy 39,29%, mean regret 0,1064. Актуальные
результаты основной модели после расширения находятся в
`models/control/negative_augmented/logistics` и составляют 50,00% / 0,0603.

Обе оценки используют один неизменный файл `logistics_dqn_zero_shot.jsonl`.
