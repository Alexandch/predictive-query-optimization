# Калибровка прогноза

Калибровка корректирует прогноз для конкретной БД без изменения весов XGBoost.
Профиль привязан к `host:port/database` и SHA-256 модели, хранится локально
и не переносится автоматически на другой источник или другую модель.

## В приложении

В расширенном режиме нажмите «Предварительно калибровать по файлу SQL» и выберите
набор характерных read-only запросов. Калибровка реально выполняет
`EXPLAIN ANALYZE`: один прогрев, затем пять замеров с ограничением времени.
В профиль попадают медиана и диапазон, а не время ожидания интерфейса.

Дальнейший быстрый прогноз применяет готовый профиль без повторного выполнения
SELECT. Точечная оценка для ранее измеренного SQL подписывается как результат
прошлых измерений; её нельзя считать независимым прогнозом нового запроса.

## Когда применяется поправка

Устойчивая сессия из пяти замеров точного SQL может использоваться сразу.
При накоплении сессий учитываются до трёх последних. Групповые поправки учитывают
JOIN, подзапросы, агрегации, сортировки и масштаб базового прогноза.

Группа требует не менее трёх разных SQL и знакомого нормализованного шаблона.
Общая групповая часть становится доступной после десяти разных SQL и применяется
только при улучшении MAE не менее чем на 0,5 мс и 2%. На незнакомую структуру
коэффициент автоматически не переносится.

MAE внутри профиля рассчитана на калибровочных наблюдениях. Независимую точность
нужно проверять отдельно на SQL, не использованном для настройки поправки.
Кэш, данные, аппаратная нагрузка и настройки PostgreSQL влияют на времена.

## CLI

```powershell
.\.venv\Scripts\pqo-calibrate.exe add models\xgboost\trial\xgboost_query_time.joblib artifacts\calibration\database.json "SELECT * FROM public.orders WHERE customer_id = 100"
.\.venv\Scripts\pqo-calibrate.exe status artifacts\calibration\database.json
.\.venv\Scripts\pqo-predict.exe models\xgboost\trial\xgboost_query_time.joblib "SELECT * FROM public.orders WHERE customer_id = 200" --calibration artifacts\calibration\database.json
```

Замените таблицу на свою. Подключение задаётся [переменными окружения](connections.md).
Команда `add` выполняет SQL; `status` читает профиль.

Для раздельной проверки калибровки и контроля:

```powershell
.\.venv\Scripts\pqo-calibrate.exe experiment dataset\postgresql\production_control.csv models\xgboost\trial\xgboost_query_time.joblib artifacts\calibration\evaluation --calibration-fraction 0.20 --seed 1701 --split-mode parameter
.\.venv\Scripts\pqo-calibrate.exe cross-validate dataset\postgresql\production_control.csv models\xgboost\trial\xgboost_query_time.joblib artifacts\calibration\cross-validation --runs 20 --seed-start 1 --split-mode both
```

`parameter` оставляет разные SQL известных семейств по обе стороны разбиения;
`unseen-template` удерживает целые семейства. Точные SQL не пересекаются.
Эти команды создают отчёты, но не заменяют активную модель приложения.
