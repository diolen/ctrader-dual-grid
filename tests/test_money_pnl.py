"""Unit-тесты конвертации monetary P/L из cTrader API."""

from app.connection.ctrader_client import _close_detail_net_pnl, _money_to_float
from types import SimpleNamespace


class TestMoneyPnl:

    def test_money_to_float(self):
        assert _money_to_float(1050, 2) == 10.5
        assert _money_to_float(123456, 5) == 1.23456

    def test_close_detail_net_pnl(self):
        cpd = SimpleNamespace(
            grossProfit=500,
            swap=-50,
            commission=-10,
            moneyDigits=2,
        )
        assert _close_detail_net_pnl(cpd, 2) == 4.4
