# Проверки проекта

## Модульные тесты

Из корня репозитория:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
```

PostgreSQL-тесты с отдельным разрешением пропускаются, пока не включён их флаг.
Такой пропуск не является успешной интеграционной проверкой. Он не мешает
модульным тестам парсера, моделей, интерфейса и политик рекомендаций.

## Подготовка локального стенда

```powershell
docker compose up -d --wait
```

Подключение берётся из [переменных окружения](connections.md), а не из профиля GUI.
Нужны доступная БД `query_optimizer` с исходными схемами и роль с CREATEDB для
тестов, создающих свои временные БД. Используйте отдельный доверенный стенд.

Для университетских тестов дополнительно нужны `pqo_university_unseen`
и `pqo_university_index_lab`. Если их ещё нет:

```powershell
.\.venv\Scripts\python.exe -m pqo.unseen_database create
.\.venv\Scripts\python.exe -m pqo.transaction_index_trial create-university-lab
```

Команды не предназначены для пересоздания существующих БД. Второй вызов
клонирует подготовленный источник; активные подключения к нему могут помешать.

## Интеграционные группы

| Флаг со значением `1` | Проверка |
| --- | --- |
| `PQO_INTEGRATION_TESTS` | базовый доступ к PostgreSQL, сбор и индексные проверки |
| `PQO_UNIVERSITY_TESTS` | планы и кандидаты университетского стенда |
| `PQO_TRANSACTION_TRIAL_TESTS` | реальные пробы SELECT на отдельной копии |
| `PQO_DISPOSABLE_REPLAY_TESTS` | последовательное выполнение и очистка собственных копий |
| `PQO_STATUS_INTEGRATION_TESTS` | процедура статусов, аудит и конкуренция |
| `PQO_CONNECTION_INTEGRATION_TESTS` | раздельные источники и служебная история |

Полный прогон после подготовки стенда:

```powershell
$env:PQO_INTEGRATION_TESTS = "1"
$env:PQO_UNIVERSITY_TESTS = "1"
$env:PQO_TRANSACTION_TRIAL_TESTS = "1"
$env:PQO_DISPOSABLE_REPLAY_TESTS = "1"
$env:PQO_STATUS_INTEGRATION_TESTS = "1"
$env:PQO_CONNECTION_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --maxfail=1
```

`scripts/verify.ps1` поднимает Docker, включает только базовый
`PQO_INTEGRATION_TESTS` и проверяет комплектные модели. Он не заменяет включение
остальных пяти групп.

## Сквозная проверка транзакций

```powershell
.\.venv\Scripts\python.exe -m pqo.transaction_acceptance --output artifacts\transaction-acceptance --confirm-trusted-laboratory-server
```

Исполнитель создаёт собственные фикстуры и одноразовые БД на трёх схемах,
проверяет индексы, замену SELECT, несовпадение результатов, отмену и очистку.
Нужен новый каталог результатов. Фактические режимы и ограничения описаны в
[transactions.md](transactions.md). Технический успех не требует положительного
ускорения каждого сценария.

## Проверка приложения

Загрузка GUI-зависимостей и моделей без анализа SQL:

```powershell
.\.venv\Scripts\python.exe -m pqo.desktop --smoke-test
```

Для чистого профиля есть отдельный SSL-стенд с двумя источниками, служебной БД
и ролью только для чтения:

```powershell
.\.venv\Scripts\python.exe scripts\clean_profile_acceptance.py --postgres-bin "C:\Program Files\PostgreSQL\18\bin" --openssl "C:\Program Files\Git\mingw64\bin\openssl.exe" --output C:\Temp\pqo-clean-check --confirm-local-laboratory
```

Укажите свои установленные binaries и новый каталог с латинским путём.
С `--executable` проверяется собранный EXE, без него — исходники. Сценарий
проверяет SSL, новый запуск и перезапуск, профили, историю, отказ служебной БД,
read-only роль, переписывание и сохранность рабочих настроек. Он останавливает
собственный кластер и оставляет отчёт; чужие серверы не открывает.
Это локальная проверка, а не испытание удалённого сервера или другого компьютера.

Для нативной Windows-проверки используйте `QT_QPA_PLATFORM=windows`:
offscreen не воспроизводит все события оконной системы. Порядок проверки
установщика приведён в [windows-packaging.md](windows-packaging.md).
