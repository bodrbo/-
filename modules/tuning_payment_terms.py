"""Canonical payment terms stored in tuning_orders.payment_terms_json.

None means unassigned. Prepayment: {"type":"prepayment","percent":"30"}.
Schedule: {"type":"schedule","payments":[{"date":"2026-12-01","amount":"10000.00"}]}.
Decimal strings preserve exact amounts; actual receipts remain tuning_payments.
"""
import datetime as dt
import json
from decimal import Decimal, InvalidOperation


class TermsError(ValueError):
    pass


def _number(value, label, minimum, maximum):
    try:
        result = Decimal(str(value).strip().replace(' ', '').replace('\xa0', '').replace(',', '.'))
        if not result.is_finite() or not minimum <= result <= maximum:
            raise InvalidOperation
        if result != result.quantize(Decimal('0.01')):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        raise TermsError(label)
    return result


def normalize_payment_terms(payload):
    if not isinstance(payload, dict):
        raise TermsError('Некорректные условия оплаты.')
    kind = payload.get('type')
    if kind == 'none':
        return None
    if kind == 'prepayment':
        percent = _number(payload.get('percent'), 'Укажите процент предоплаты от 0 до 100, не более двух знаков после запятой.', Decimal('0'), Decimal('100'))
        return {'type': kind, 'percent': format(percent.normalize(), 'f')}
    if kind != 'schedule':
        raise TermsError('Выберите предоплату или график платежей.')
    payments = payload.get('payments')
    if not isinstance(payments, list) or not 1 <= len(payments) <= 100:
        raise TermsError('В графике должно быть от 1 до 100 платежей.')
    result = []
    for index, row in enumerate(payments, 1):
        if not isinstance(row, dict):
            raise TermsError(f'Платёж №{index}: проверьте дату и сумму.')
        try:
            date = dt.date.fromisoformat(str(row.get('date', ''))).isoformat()
        except ValueError:
            raise TermsError(f'Платёж №{index}: укажите корректную дату.')
        amount = _number(row.get('amount'), f'Платёж №{index}: сумма должна быть больше нуля, не более 999 999 999,99 ₽ и с точностью до копеек.', Decimal('0.01'), Decimal('999999999.99'))
        result.append({'date': date, 'amount': format(amount, '.2f')})
    result.sort(key=lambda row: row['date'])
    return {'type': kind, 'payments': result}


def read_payment_terms(order):
    """Read a sqlite Row or dict. Old/unassigned orders return None."""
    if 'payment_terms_json' not in order.keys() or not order['payment_terms_json']:
        return None
    try:
        return normalize_payment_terms(json.loads(order['payment_terms_json']))
    except (ValueError, TypeError):
        return None


def payment_terms_label(terms):
    if not terms:
        return 'Назначить'
    if terms['type'] == 'prepayment':
        return 'Предоплата ' + terms['percent'].replace('.', ',') + '%'
    return 'График платежей'
