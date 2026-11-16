"""Closed PostgreSQL transaction-control grammar, independent of SQLGlot AST gaps."""
from dataclasses import dataclass, replace
import re

from sqlglot import Dialect, exp
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType


ISOLATION_LEVELS = frozenset({'READ UNCOMMITTED', 'READ COMMITTED', 'REPEATABLE READ', 'SERIALIZABLE'})
CONTROL_KINDS = frozenset({'set_transaction', 'savepoint', 'rollback_to', 'release'})
_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
_POSTGRES = Dialect.get_or_raise('postgres')
_CONTROL_WORD_TOKENS = frozenset({TokenType.COMMA, *(
    _POSTGRES.tokenize(word)[0].token_type for word in
    'BEGIN START TRANSACTION SET ISOLATION LEVEL READ UNCOMMITTED COMMITTED REPEATABLE SERIALIZABLE ONLY WRITE NOT DEFERRABLE WORK COMMIT END ROLLBACK ABORT TO SAVEPOINT RELEASE'.split())})


@dataclass(frozen=True)
class TransactionOptions:
    isolation_level: str | None = None
    read_only: bool | None = None
    deferrable: bool | None = None

    def __post_init__(self):
        if self.isolation_level is not None and self.isolation_level not in ISOLATION_LEVELS:
            raise ValueError('Неизвестный уровень изоляции PostgreSQL.')
        if any(value is not None and type(value) is not bool for value in (self.read_only, self.deferrable)):
            raise ValueError('Режимы транзакции должны быть логическими значениями.')

    def overlay(self, other):
        return replace(self, **{key: value for key, value in vars(other).items() if value is not None})

    def resolve(self, defaults):
        return defaults.overlay(self)

    @property
    def clauses(self):
        return tuple(value for value in (
            'ISOLATION LEVEL '+self.isolation_level if self.isolation_level else None,
            ('READ ONLY' if self.read_only else 'READ WRITE') if self.read_only is not None else None,
            ('DEFERRABLE' if self.deferrable else 'NOT DEFERRABLE') if self.deferrable is not None else None,
        ) if value is not None)

    @property
    def begin_sql(self):
        return 'BEGIN'+(' '+', '.join(self.clauses) if self.clauses else '')

    @property
    def description(self):
        level = self.isolation_level or 'по умолчанию эталона'
        if self.isolation_level == 'READ UNCOMMITTED':
            level += ' (видимость как READ COMMITTED в PostgreSQL)'
        access = 'по умолчанию' if self.read_only is None else 'READ ONLY' if self.read_only else 'READ WRITE'
        deferred = 'по умолчанию' if self.deferrable is None else 'да' if self.deferrable else 'нет'
        if self.deferrable and not (self.isolation_level=='SERIALIZABLE' and self.read_only):
            deferred += ' (влияет только на SERIALIZABLE READ ONLY)'
        return f'Изоляция: {level}; доступ: {access}; DEFERRABLE: {deferred}.'


@dataclass(frozen=True)
class TransactionControl:
    kind: str
    sql: str
    options: TransactionOptions | None = None
    savepoint: str | None = None


def split_transaction_sql(text):
    """Token positions retain literals/comments, including dollar-quoted semicolons."""
    try:
        tokens = _POSTGRES.tokenize(text)
    except TokenError:
        raise ValueError('Не удалось разобрать SQL транзакции; ничего не выполнялось.') from None
    statements, current = [], []
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            if current:
                statements.append(text[current[0].start:current[-1].end+1])
                current = []
        else:
            current.append(token)
    if current:
        statements.append(text[current[0].start:current[-1].end+1])
    return tuple(statements)


def _words(tokens):
    if any(token.token_type not in _CONTROL_WORD_TOKENS
            or not re.fullmatch(r'[A-Za-z]+|,', token.text) for token in tokens):
        raise ValueError('Некорректные параметры управляющей команды транзакции.')
    return [token.text.upper() for token in tokens]


def _options(tokens):
    words, values = _words(tokens), {}
    choices = [(('ISOLATION','LEVEL',*level.split()),'isolation_level',level) for level in ISOLATION_LEVELS]
    choices += [(('READ','ONLY'),'read_only',True),(('READ','WRITE'),'read_only',False),
                (('NOT','DEFERRABLE'),'deferrable',False),(('DEFERRABLE',),'deferrable',True)]
    position = 0
    while position < len(words):
        match = next((item for item in choices if tuple(words[position:position+len(item[0])]) == item[0]), None)
        if match is None or match[1] in values:
            raise ValueError('Неизвестный или повторный режим транзакции; SNAPSHOT/SESSION не поддерживаются.')
        phrase, key, value = match
        values[key] = value
        position += len(phrase)
        if position < len(words) and words[position] == ',':
            position += 1
            if position == len(words):
                raise ValueError('После запятой нужен режим транзакции.')
    if not values:
        raise ValueError('Не заданы параметры SET TRANSACTION.')
    return TransactionOptions(**values)


def parse_transaction_control(text):
    tokens = _POSTGRES.tokenize(text)
    if not tokens or tokens[0].token_type not in _CONTROL_WORD_TOKENS:
        return None
    first = tokens[0].text.upper()
    if first in {'BEGIN','START'}:
        words = _words(tokens)
        position = 1
        if first == 'START':
            if words[1:2] != ['TRANSACTION']:
                return None
            position += 1
        elif words[1:2] in (['WORK'], ['TRANSACTION']):
            position += 1
        options = _options(tokens[position:]) if position < len(tokens) else TransactionOptions()
        return TransactionControl('begin',options.begin_sql,options)
    if first == 'SET' and len(tokens)>1 and tokens[1].text.upper() == 'TRANSACTION':
        _words(tokens[:2])
        options = _options(tokens[2:])
        return TransactionControl('set_transaction','SET TRANSACTION '+', '.join(options.clauses),options)
    if first in {'COMMIT','END','ROLLBACK','ABORT'}:
        if first == 'ROLLBACK':
            position = 2 if len(tokens)>1 and tokens[1].text.upper() in {'WORK','TRANSACTION'} else 1
            if len(tokens)>position and tokens[position].text.upper()=='TO':
                position += 1
                if len(tokens)>position and tokens[position].text.upper()=='SAVEPOINT':position += 1
                return _savepoint_control('rollback_to',tokens,position)
        words = _words(tokens)
        if len(words)==1 or len(words)==2 and words[1] in {'WORK','TRANSACTION'}:
            kind = 'commit' if first in {'COMMIT','END'} else 'rollback'
            return TransactionControl(kind,kind.upper())
        raise ValueError('CHAIN, вложенные транзакции и дополнительные параметры завершения не поддерживаются.')
    if first in {'SAVEPOINT','RELEASE'}:
        position = 2 if first=='RELEASE' and len(tokens)>1 and tokens[1].text.upper()=='SAVEPOINT' else 1
        return _savepoint_control('savepoint' if first=='SAVEPOINT' else 'release',tokens,position)
    return None


def _savepoint_control(kind, tokens, position):
    _words(tokens[:position])
    if len(tokens)!=position+1 or tokens[position].token_type not in {TokenType.VAR,TokenType.IDENTIFIER}:
        raise ValueError('Нужно одно простое имя точки сохранения.')
    token = tokens[position]
    if not _NAME.fullmatch(token.text):
        raise ValueError('Имя точки сохранения: до 63 ASCII-символов.')
    quoted = token.token_type == TokenType.IDENTIFIER
    name = token.text if quoted else token.text.lower()
    identifier = exp.Identifier(this=name,quoted=quoted).sql(dialect='postgres')
    prefix = {'savepoint':'SAVEPOINT','rollback_to':'ROLLBACK TO SAVEPOINT','release':'RELEASE SAVEPOINT'}[kind]
    return TransactionControl(kind,prefix+' '+identifier,savepoint=name)


def read_transaction_options(connection, *, defaults=False):
    """SHOW avoids acquiring the user transaction's first MVCC snapshot."""
    prefix = 'default_transaction_' if defaults else 'transaction_'
    level = connection.execute('SHOW '+prefix+'isolation').fetchone()[0].upper()
    flags = [connection.execute('SHOW '+prefix+name).fetchone()[0] for name in ('read_only','deferrable')]
    if any(flag not in ('on','off') for flag in flags):
        raise RuntimeError('PostgreSQL вернул неизвестный режим транзакции.')
    return TransactionOptions(level,flags[0]=='on',flags[1]=='on')
