"""Statement and whole-program rewrite comparisons on disposable copies."""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json

from sqlglot import exp, parse

from .analysis_control import AnalysisControl
from .transaction_comparison import source_context_digest, sql_digest
from .transaction_replay_guard import ReplayProgram, prepare_replay
from .transaction_replay import TransactionReplay, _run_disposable_programs, validate_reference
from .transaction_mixed_recommendation import MixedReplayPolicy, assess_mixed_replay
from .sql_rewrite import generate_sql_rewrites, _rewrite_or_equalities, _rewrite_ranges
from .transaction_cte import physical_tables


RULES = frozenset({'or-equality-to-in','range-to-between','subquery-order-without-limit',
    'non-aggregate-having-filter','inline-reused-cte','distinct-inner-join-to-exists','in-filter-to-exists'})


@dataclass(frozen=True)
class SelectRewriteOption:
    number: int | None
    rule_id: str
    title: str
    original_sql: str
    variant_sql: str


@dataclass(frozen=True)
class SelectReplacement:
    number: int | None
    original: ReplayProgram
    variant: ReplayProgram

    @property
    def original_sql(self):
        if self.number is None:
            return _render(self.original)
        return self.original.commands[self.number-1].sql

    @property
    def variant_sql(self):
        if self.number is None:
            return _render(self.variant)
        return self.variant.commands[self.number-1].sql

    @property
    def changed_numbers(self):
        return tuple(a.number for a, b in zip(self.original.commands, self.variant.commands, strict=True) if a != b)


@dataclass(frozen=True)
class TransactionSelectRewrite:
    number: int | None
    measurement: TransactionReplay  # no SQL/rows retained in this report


def _render(program):
    return program.options.begin_sql+';\n'+';\n'.join(command.sql for command in program.commands)+';\n'+program.ending.upper()+';'


def _prepare_program(text,settings):
    try:
        return prepare_replay(text,settings,allow_select_wrapper=True)
    except ValueError as exc:
        # The inspector's chained parser errors can contain SQL/literals. Keep
        # its safe outer explanation, not that chain in the worker's error log.
        raise ValueError(str(exc)) from None
    except Exception:
        raise ValueError('SQL не удалось подготовить в ограниченном режиме; ничего не выполнялось.') from None


def _check_statement_replacement(current, tree):
    if type(current) is not type(tree):
        raise ValueError('Тип каждой команды должен остаться прежним.')
    if type(current) is exp.Select:
        if any(tree.args.get(key) != current.args.get(key) for key in ('order', 'limit')):
            raise ValueError('Внешние ORDER BY и LIMIT должны остаться неизменными, включая направление/NULLS.')
        sources = lambda node: sorted((t.db, t.name) for t in physical_tables(node))
        if sources(tree) != sources(current):
            raise ValueError('Физические таблицы выбранного SELECT и число их вхождений не должны меняться.')
        return
    if type(current) in (exp.Update, exp.Delete):
        if (not current.args.get('where') or not tree.args.get('where')
                or any(tree.args.get(key) != current.args.get(key) for key in current.args.keys() | tree.args.keys()
                       if key != 'where')):
            raise ValueError('В UPDATE/DELETE можно изменить только существующее условие WHERE.')
        if sorted((t.db, t.name) for t in physical_tables(tree)) != sorted((t.db, t.name) for t in physical_tables(current)):
            raise ValueError('В условии UPDATE/DELETE нельзя менять состав физических источников.')
        return
    raise ValueError('Заменять можно SELECT и условия WHERE в UPDATE/DELETE.')


def _prepare_transaction_replacement(original, settings, text):
    variant = _prepare_program(text, settings)
    if (variant.options != original.options or variant.ending != original.ending or variant.tables != original.tables
            or len(variant.commands) != len(original.commands)):
        raise ValueError('Режим, завершение, число команд и состав таблиц должны остаться прежними.')
    prepared = SelectReplacement(None, original, variant)
    if not prepared.changed_numbers:
        raise ValueError('Вариант совпадает с исходной транзакцией.')
    for a, b in zip(original.commands, variant.commands, strict=True):
        if a != b:
            if a.kind != b.kind or a.kind not in {'select', 'update', 'delete'}:
                raise ValueError('Нельзя менять тип, порядок управляющих команд или INSERT.')
            _check_statement_replacement(parse(a.sql, read='postgres')[0], parse(b.sql, read='postgres')[0])
    return prepared


def prepare_select_replacement(text, settings, number, variant_sql):
    """Fail before any connection; no DML edits, insertion, deletion or reordering."""
    original = _prepare_program(text,settings)
    if number is None:
        if not isinstance(variant_sql, str) or not variant_sql.strip():
            raise ValueError('Введите полный вариант транзакции.')
        return _prepare_transaction_replacement(original, settings, variant_sql)
    if type(number) is not int or not 1 <= number <= len(original.commands) or original.commands[number-1].kind != 'select':
        raise ValueError('Выберите номер самостоятельного SELECT, не команду записи.')
    if not isinstance(variant_sql,str) or not variant_sql.strip() or len(variant_sql)>50_000:
        raise ValueError('Нужен один SELECT до 50000 символов.')
    try:
        nodes = [node for node in parse(variant_sql,read='postgres') if node is not None and not isinstance(node,exp.Semicolon)]
    except Exception:
        raise ValueError('Вариант SELECT не разобран.') from None
    if len(nodes) != 1 or type(nodes[0]) is not exp.Select:
        raise ValueError('Замена должна содержать ровно один SELECT, без DML/BEGIN/COMMIT.')
    tree = nodes[0]
    current = parse(original.commands[number-1].sql,read='postgres')[0]
    if tree == current:
        raise ValueError('Вариант совпадает с исходным SELECT; измените его или выберите правило.')
    _check_statement_replacement(current, tree)
    commands = list(original.commands)
    commands[number-1] = replace(commands[number-1],sql=tree.sql(dialect='postgres'))
    variant = _prepare_program(_render(replace(original,commands=tuple(commands))),settings)
    if variant.tables != original.tables:
        raise ValueError('Замена должна обращаться к тем же физическим таблицам.')
    if any(a != b for i,(a,b) in enumerate(zip(original.commands,variant.commands),1) if i != number):
        raise ValueError('Изменены команды вне выбранного SELECT.')
    return SelectReplacement(number,original,variant)


def generate_transaction_select_rewrites(text, settings, *, control=None, include_transaction=False):
    """Local generation of guarded single rules plus a manual option per SELECT."""
    control = control or AnalysisControl()
    control.check()
    program = _prepare_program(text,settings)
    options = []
    for command in program.commands:
        control.check()
        if command.kind != 'select':
            continue
        for candidate in generate_sql_rewrites(command.sql,max_combined_candidates=0,include_window_materialization=False):
            if candidate.rule_id not in RULES:
                continue
            if candidate.rule_id == 'inline-reused-cte':
                source = parse(command.sql, read='postgres')[0]
                if any(cte.find(exp.Limit) is not None for cte in source.find_all(exp.CTE)):
                    continue  # Multiple LIMIT evaluations may pick different tied rows.
            try:
                prepared = prepare_select_replacement(text,settings,command.number,candidate.sql_text)
            except ValueError:
                continue  # a rule outside this closed execution contract is not executable
            options.append(SelectRewriteOption(command.number,candidate.rule_id,candidate.title,
                command.sql,prepared.variant_sql))
        options.append(SelectRewriteOption(command.number,'manual','Ручной вариант (измените SELECT справа)',command.sql,command.sql))
    if include_transaction:
        commands = list(program.commands)
        for command in program.commands:
            control.check()
            if command.kind in {'update', 'delete'}:
                tree = parse(command.sql, read='postgres')[0]
                predicate = tree.args.get('where')
                if predicate is None:
                    continue
                predicate, or_changed = _rewrite_or_equalities(predicate)
                predicate, range_changed = _rewrite_ranges(predicate)
                if or_changed or range_changed:
                    tree.set('where', predicate)
                    commands[command.number - 1] = replace(command, sql=tree.sql(dialect='postgres'))
            elif command.kind == 'select':
                option = next((item for item in options if item.number == command.number and item.rule_id != 'manual'), None)
                if option:
                    commands[command.number - 1] = replace(command, sql=option.variant_sql)
        original_sql = _render(program)
        try:
            combined = _prepare_transaction_replacement(program, settings, _render(replace(program, commands=tuple(commands))))
        except ValueError:
            pass
        else:
            count = len(combined.changed_numbers)
            options.insert(0, SelectRewriteOption(None, 'whole-transaction', f'Вся транзакция · изменено команд: {count}',
                original_sql, combined.variant_sql))
        options.append(SelectRewriteOption(None, 'manual', 'Вся транзакция · ручной вариант', original_sql, original_sql))
    return tuple(options)


def select_rewrite_request_digest(text, settings, reference, number, variant_sql, series, policy):
    normalized = (prepare_select_replacement(text, settings, None, variant_sql).variant_sql if number is None
                  else parse(variant_sql,read='postgres')[0].sql(dialect='postgres'))
    payload = ('transaction-select-rewrite-v1',sql_digest(text),source_context_digest(settings),reference,
        number,sql_digest(normalized),series,asdict(policy))
    return sha256(json.dumps(payload,sort_keys=True,ensure_ascii=True).encode()).hexdigest()


def run_transaction_select_rewrite(text, settings, reference, number, variant_sql, *, series=2,
        test_copy_confirmed=False, disposable_databases_confirmed=False, policy=None, control=None, recovery_directory=None):
    control = control or AnalysisControl()
    control.check()
    validate_reference(settings,reference,test_copy_confirmed,disposable_databases_confirmed)
    if type(series) is not int or not 1 <= series <= 3:
        raise ValueError('Допускается 1–3 серии; одна серия диагностическая.')
    policy = policy if policy is not None else MixedReplayPolicy()
    if not isinstance(policy,MixedReplayPolicy):
        raise ValueError('Пороговая политика должна быть зафиксирована до пробы.')
    prepared = prepare_select_replacement(text,settings,number,variant_sql)
    digest = select_rewrite_request_digest(text,settings,reference,number,prepared.variant_sql,series,policy)
    report = _run_disposable_programs(prepared.original,settings,reference,(),series,control,recovery_directory,
        candidate_program=prepared.variant)
    report = replace(report,request_digest=digest,policy=policy)
    report = replace(report,recommendation=assess_mixed_replay(report,policy,expected_request_digest=digest,mode='rewrite'))
    return TransactionSelectRewrite(number,report)
