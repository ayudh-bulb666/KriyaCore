import csv
import io
from datetime import date, timedelta

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, Response)
from flask_login import login_required, current_user
from sqlalchemy import func

from .models import db, Expense, EXPENSE_CATEGORIES, EXPENSE_CATEGORY_LABELS
from .helpers import role_required

expenses_bp = Blueprint('expenses', __name__, url_prefix='/<string:gym_slug>/expenses')


def _month_bounds(ref=None):
    """First and last day of the month containing `ref` (default: today)."""
    ref   = ref or date.today()
    start = ref.replace(day=1)
    nxt   = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def _parse_month(raw):
    """Read a 'YYYY-MM' string off the query string. Anything unparseable
    falls back to the current month rather than erroring — a bad URL should
    show this month's costs, not a stack trace."""
    if raw:
        try:
            y, m = raw.split('-')
            return _month_bounds(date(int(y), int(m), 1))
        except (ValueError, TypeError):
            pass
    return _month_bounds()


@expenses_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    """Expenses are the owner's business, not the front desk's — role-gated
    to super_admin for the same reason staff don't see revenue."""
    gid          = current_user.gym_id
    month_raw    = request.args.get('month', '')
    start, end   = _parse_month(month_raw)

    rows = (Expense.query
            .filter(Expense.gym_id == gid,
                    Expense.incurred_on >= start,
                    Expense.incurred_on <= end)
            .order_by(Expense.incurred_on.desc(), Expense.id.desc())
            .all())

    total = sum(r.amount for r in rows)

    # Per-category totals, biggest first — the owner wants to know what the
    # money went on before they care about individual line items.
    by_category = {}
    for r in rows:
        by_category[r.category] = by_category.get(r.category, 0.0) + r.amount
    breakdown = sorted(
        ({'key': k, 'label': EXPENSE_CATEGORY_LABELS.get(k, 'Other'),
          'amount': v, 'share': (v / total * 100) if total else 0}
         for k, v in by_category.items()),
        key=lambda d: d['amount'], reverse=True,
    )

    # Last 6 months for the month picker.
    months = []
    cur = date.today().replace(day=1)
    for _ in range(6):
        months.append(cur)
        cur = (cur - timedelta(days=1)).replace(day=1)

    return render_template(
        'expenses/index.html',
        rows=rows,
        total=total,
        breakdown=breakdown,
        categories=EXPENSE_CATEGORIES,
        month_start=start,
        month_value=start.strftime('%Y-%m'),
        months=months,
        today=date.today().isoformat(),
    )


@expenses_bp.route('/new', methods=['POST'])
@login_required
@role_required('super_admin')
def new():
    gid      = current_user.gym_id
    category = request.form.get('category', 'other')
    raw_amt  = (request.form.get('amount') or '').strip().replace(',', '')
    date_str = request.form.get('incurred_on', '')
    note     = request.form.get('note', '').strip()

    if category not in EXPENSE_CATEGORY_LABELS:
        flash('Pick a category for this expense.', 'danger')
        return redirect(url_for('expenses.index'))

    try:
        amount = float(raw_amt)
    except ValueError:
        flash('Amount must be a number.', 'danger')
        return redirect(url_for('expenses.index'))

    if amount <= 0:
        flash('An expense has to be more than zero.', 'danger')
        return redirect(url_for('expenses.index'))

    try:
        incurred_on = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        flash('That date isn\'t valid.', 'danger')
        return redirect(url_for('expenses.index'))

    if incurred_on > date.today():
        flash('You can\'t record an expense in the future.', 'danger')
        return redirect(url_for('expenses.index'))

    db.session.add(Expense(
        gym_id=gid,
        category=category,
        amount=round(amount, 2),
        incurred_on=incurred_on,
        note=note or None,
        recorded_by_name=current_user.name,
    ))
    db.session.commit()

    flash(f'₹{amount:,.0f} recorded under {EXPENSE_CATEGORY_LABELS[category]}.', 'success')
    return redirect(url_for('expenses.index', month=incurred_on.strftime('%Y-%m')))


@expenses_bp.route('/<int:expense_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(expense_id):
    exp = Expense.query.filter_by(id=expense_id, gym_id=current_user.gym_id).first_or_404()
    month = exp.incurred_on.strftime('%Y-%m')
    label, amount = exp.category_label, exp.amount

    db.session.delete(exp)
    db.session.commit()

    flash(f'Deleted the ₹{amount:,.0f} {label} entry.', 'info')
    return redirect(url_for('expenses.index', month=month))


@expenses_bp.route('/export.csv')
@login_required
@role_required('super_admin')
def export_csv():
    gid        = current_user.gym_id
    start, end = _parse_month(request.args.get('month', ''))

    rows = (Expense.query
            .filter(Expense.gym_id == gid,
                    Expense.incurred_on >= start,
                    Expense.incurred_on <= end)
            .order_by(Expense.incurred_on.asc())
            .all())

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Date', 'Category', 'Amount', 'Note', 'Recorded by'])
    for r in rows:
        w.writerow([r.incurred_on.isoformat(), r.category_label,
                    f'{r.amount:.2f}', r.note or '', r.recorded_by_name or ''])
    w.writerow([])
    w.writerow(['', 'Total', f'{sum(r.amount for r in rows):.2f}', '', ''])

    out.seek(0)
    return Response(
        out.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment; filename=expenses-{start.strftime("%Y-%m")}.csv'},
    )
