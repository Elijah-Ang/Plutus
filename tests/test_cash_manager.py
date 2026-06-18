from app.cash_manager import CashManager, calculate_cash_recommendation


def test_profit_lock_calculation():
    result = calculate_cash_recommendation(100, 100, 20, 1100, 1000, {"profit_lock_rate": .3, "cash_reserve_rate": .2, "reinvest_rate": .5, "minimum_withdrawal": 10})
    assert result.suggested_withdrawal == 30
    assert result.reserve == 20
    assert result.reinvest == 50


def test_withdrawal_disabled():
    try:
        CashManager({}).withdraw(10)
    except PermissionError:
        pass
    else:
        raise AssertionError("withdraw must always be disabled")
