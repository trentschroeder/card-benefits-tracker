from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


PERIOD_LABELS = {
    'monthly':     'Monthly',
    'quarterly':   'Quarterly',
    'semi-annual': 'Every 6 Months',
    'annual':      'Annually',
}


def get_current_period(period_type, for_date=None):
    d = for_date or date.today()

    if period_type == 'monthly':
        period_start = d.replace(day=1)
        period_end = (period_start + relativedelta(months=1)) - timedelta(days=1)

    elif period_type == 'quarterly':
        quarter = (d.month - 1) // 3
        period_start = date(d.year, quarter * 3 + 1, 1)
        period_end = (period_start + relativedelta(months=3)) - timedelta(days=1)

    elif period_type == 'semi-annual':
        half = (d.month - 1) // 6
        period_start = date(d.year, half * 6 + 1, 1)
        period_end = (period_start + relativedelta(months=6)) - timedelta(days=1)

    else:  # annual
        period_start = date(d.year, 1, 1)
        period_end = date(d.year, 12, 31)

    return period_start, period_end


def days_left(period_end):
    return (period_end - date.today()).days
