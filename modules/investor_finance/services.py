"""Persistence and reporting for investor finance history."""

import datetime as dt


LEGACY_SOURCE_TYPE = "legacy_xlsx"


def import_legacy_workbook(db, parsed, *, boat, investor_name, now=None):
    """Replace one boat's legacy workbook rows atomically and idempotently."""
    now = now or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "DELETE FROM investor_ledger_entries WHERE boat = ? AND source_type = ?",
        (boat, LEGACY_SOURCE_TYPE),
    )
    db.execute(
        "DELETE FROM investor_distributions WHERE boat = ? AND source_type = ?",
        (boat, LEGACY_SOURCE_TYPE),
    )
    for entry in parsed["entries"]:
        db.execute(
            "INSERT INTO investor_ledger_entries "
            "(boat, investor_name, entry_date, entry_kind, description, amount, "
            "source_type, source_row, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                boat,
                investor_name,
                entry["date"],
                entry["kind"],
                entry["description"],
                entry["amount"],
                LEGACY_SOURCE_TYPE,
                entry["source_row"],
                now,
            ),
        )
    for payout in parsed["payouts"]:
        db.execute(
            "INSERT INTO investor_distributions "
            "(boat, investor_name, payout_date, amount, source_type, source_row, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                boat,
                investor_name,
                payout["date"],
                payout["amount"],
                LEGACY_SOURCE_TYPE,
                payout["source_row"],
                now,
            ),
        )
    db.execute(
        "INSERT INTO investor_asset_profiles "
        "(boat, investor_name, investment_date, investment_amount, investor_share, "
        "financial_year_days, season_months, manager_commission_note, history_through_date, "
        "source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(boat) DO UPDATE SET investor_name = excluded.investor_name, "
        "investment_date = excluded.investment_date, investment_amount = excluded.investment_amount, "
        "investor_share = excluded.investor_share, financial_year_days = excluded.financial_year_days, "
        "season_months = excluded.season_months, manager_commission_note = excluded.manager_commission_note, "
        "history_through_date = excluded.history_through_date, source_type = excluded.source_type, "
        "updated_at = excluded.updated_at",
        (
            boat,
            investor_name,
            parsed["investment_date"],
            parsed["investment_amount"],
            parsed["investor_share"],
            parsed["financial_year_days"],
            parsed["season_months"],
            parsed["manager_commission_note"],
            parsed["history_through_date"],
            LEGACY_SOURCE_TYPE,
            now,
            now,
        ),
    )
    return {
        "entries": len(parsed["entries"]),
        "payouts": len(parsed["payouts"]),
        "warnings": list(parsed.get("warnings") or []),
    }


def _placeholders(values):
    return ",".join("?" for _ in values)


def _profile_map(db, boats):
    if not boats:
        return {}
    rows = db.execute(
        f"SELECT * FROM investor_asset_profiles WHERE boat IN ({_placeholders(boats)})",
        list(boats),
    ).fetchall()
    return {row["boat"]: dict(row) for row in rows}


def _eligible_system_trips(db, boats, profiles):
    if not boats:
        return []
    rows = db.execute(
        f"SELECT * FROM trips WHERE boat IN ({_placeholders(boats)})",
        list(boats),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        cutoff = (profiles.get(item["boat"]) or {}).get("history_through_date")
        if cutoff and item["trip_date"] <= cutoff:
            continue
        result.append(item)
    return result


def _legacy_rows(db, boats):
    if not boats:
        return []
    return [
        dict(row)
        for row in db.execute(
            f"SELECT * FROM investor_ledger_entries WHERE boat IN ({_placeholders(boats)})",
            list(boats),
        ).fetchall()
    ]


def _month_options(legacy_rows, system_trips, today):
    keys = {today.strftime("%Y-%m")}
    keys.update(row["entry_date"][:7] for row in legacy_rows)
    keys.update(row["trip_date"][:7] for row in system_trips)
    month_names = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    current_key = today.strftime("%Y-%m")
    months = []
    for key in sorted(keys, reverse=True):
        year, month = (int(part) for part in key.split("-"))
        months.append({
            "key": key,
            "label": f"{month_names[month - 1].capitalize()} {year}",
            "is_current": key == current_key,
        })
    return months, current_key


def _legacy_display_row(row, share):
    is_expense = row["entry_kind"] == "expense"
    signed_amount = -row["amount"] if is_expense else row["amount"]
    return {
        "id": f"history-{row['id']}",
        "boat": row["boat"],
        "trip_date": row["entry_date"],
        "trip_time": None,
        "work_type": row["description"],
        "revenue": 0.0 if is_expense else row["amount"],
        "commission_pct": 0.0,
        "commission_amount": 0.0,
        "labor_cost": 0.0,
        "fuel_cost": 0.0,
        "mooring_cost": 0.0,
        "extra_total": row["amount"] if is_expense else 0.0,
        "remainder": signed_amount,
        "investor_payout": signed_amount * share,
        "my_share": signed_amount * (1 - share),
        "is_expense": 1 if is_expense else 0,
        "is_historical": True,
        "source_row": row.get("source_row") or 0,
    }


def _empty_boat_totals():
    return {
        "revenue": 0.0,
        "commission": 0.0,
        "labor": 0.0,
        "fuel": 0.0,
        "mooring": 0.0,
        "extra": 0.0,
        "remainder": 0.0,
        "investor_payout": 0.0,
        "my_share": 0.0,
        "count": 0,
    }


def _aggregate_display_rows(rows):
    by_boat = {}
    for row in rows:
        totals = by_boat.setdefault(row["boat"], _empty_boat_totals())
        totals["revenue"] += row["revenue"]
        totals["commission"] += row["commission_amount"]
        totals["labor"] += row["labor_cost"]
        totals["fuel"] += row["fuel_cost"]
        totals["mooring"] += row["mooring_cost"]
        totals["extra"] += row["extra_total"]
        totals["remainder"] += row["remainder"]
        totals["investor_payout"] += row["investor_payout"]
        totals["my_share"] += row["my_share"]
        if not row["is_expense"]:
            totals["count"] += 1
    return by_boat


def build_dashboard_data(db, investor_name, boats, selected_month=None, today=None):
    """Merge legacy ledger with post-cutoff system trips and calculate returns."""
    today = today or dt.date.today()
    profiles = _profile_map(db, boats)
    legacy_rows = _legacy_rows(db, boats)
    system_trips = _eligible_system_trips(db, boats, profiles)
    months, current_key = _month_options(legacy_rows, system_trips, today)
    selected_month = selected_month or current_key

    display_rows = []
    for row in legacy_rows:
        share = (profiles.get(row["boat"]) or {}).get("investor_share", 0.5)
        display_rows.append(_legacy_display_row(row, share))
    display_rows.extend({**row, "is_historical": False, "source_row": 0} for row in system_trips)
    if selected_month != "all":
        display_rows = [row for row in display_rows if row["trip_date"][:7] == selected_month]
    display_rows.sort(
        key=lambda row: (row["trip_date"], row.get("trip_time") or "", row.get("source_row") or 0),
        reverse=True,
    )
    by_boat = _aggregate_display_rows(display_rows)

    asset_metrics = []
    for boat in boats:
        profile = profiles.get(boat)
        if not profile:
            continue
        historical_net = sum(
            row["amount"] if row["entry_kind"] == "income" else -row["amount"]
            for row in legacy_rows if row["boat"] == boat
        )
        live_profit_share = sum(
            row["investor_payout"] for row in system_trips if row["boat"] == boat
        )
        profit_share = historical_net * profile["investor_share"] + live_profit_share
        investment_amount = profile["investment_amount"]
        profitability = profit_share / investment_amount if investment_amount else None
        investment_date = dt.date.fromisoformat(profile["investment_date"])
        elapsed_days = max((today - investment_date).days, 1)
        annual_yield = (
            profitability * profile["financial_year_days"] / elapsed_days
            if profitability is not None else None
        )
        asset_metrics.append({
            **profile,
            "profit_share": profit_share,
            "profitability": profitability,
            "annual_yield": annual_yield,
            "elapsed_days": elapsed_days,
        })

    investment_total = sum(item["investment_amount"] for item in asset_metrics)
    profit_share_total = sum(item["profit_share"] for item in asset_metrics)
    profitability_total = profit_share_total / investment_total if investment_total else None
    annual_yield_total = (
        sum(item["annual_yield"] * item["investment_amount"] for item in asset_metrics)
        / investment_total
        if investment_total and all(item["annual_yield"] is not None for item in asset_metrics)
        else None
    )
    investment_metrics = None
    if asset_metrics:
        investment_metrics = {
            "investment_amount": investment_total,
            "profit_share": profit_share_total,
            "profitability": profitability_total,
            "annual_yield": annual_yield_total,
            "as_of_date": today.isoformat(),
            "investment_date": min(item["investment_date"] for item in asset_metrics),
            "investor_share": asset_metrics[0]["investor_share"] if len(asset_metrics) == 1 else None,
            "financial_year_days": asset_metrics[0]["financial_year_days"] if len(asset_metrics) == 1 else None,
        }

    distributions = []
    if boats:
        distributions = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM investor_distributions WHERE boat IN ({_placeholders(boats)}) "
                "ORDER BY payout_date DESC, id DESC",
                list(boats),
            ).fetchall()
        ]

    return {
        "months": months,
        "selected_month": selected_month,
        "by_boat": by_boat,
        "trips": display_rows,
        "grand_revenue": sum(item["revenue"] for item in display_rows),
        "grand_payout": sum(item["investor_payout"] for item in display_rows),
        "investment_metrics": investment_metrics,
        "asset_metrics": asset_metrics,
        "distributions": distributions,
        "distribution_total": sum(item["amount"] for item in distributions),
    }
