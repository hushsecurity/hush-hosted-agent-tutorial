"""Expenses kept in memory, per account, and a log of every tool call.

Everything resets when the app restarts: it's a demo.
"""

import collections
import dataclasses
import datetime
import itertools
import logging

log = logging.getLogger("expenses")

SAMPLE_EXPENSES = [
    ("Taxi to the airport", 42.50, "Travel"),
    ("Team lunch", 118.20, "Meals"),
    ("Conference ticket", 399.00, "Training"),
]


@dataclasses.dataclass
class Expense:
    id: int
    description: str
    amount: float
    category: str
    date: str


@dataclasses.dataclass
class Call:
    time: str
    account: str
    tool: str
    arguments: dict
    result: str


class Store:
    def __init__(self, max_calls: int = 200):
        self._accounts: dict[str, list[Expense]] = {}
        self._ids = itertools.count(1001)
        self.calls: collections.deque[Call] = collections.deque(maxlen=max_calls)

    def expenses(self, account: str) -> list[Expense]:
        """The account's expenses. A new account starts with a few samples."""
        if account not in self._accounts:
            self._accounts[account] = [self._new(*sample) for sample in SAMPLE_EXPENSES]
        return self._accounts[account]

    def submit(self, account: str, description: str, amount: float, category: str) -> Expense:
        expense = self._new(description, amount, category)
        self.expenses(account).append(expense)
        return expense

    def delete(self, account: str, expense_id: int) -> bool:
        expenses = self.expenses(account)
        for expense in expenses:
            if expense.id == expense_id:
                expenses.remove(expense)
                return True
        return False

    def record(self, account: str, tool: str, arguments: dict, result: str) -> None:
        self.calls.append(Call(_now(), account, tool, arguments, result))
        log.info("%s for %s%s: %s", tool, account, f" {arguments}" if arguments else "", result)

    def _new(self, description: str, amount: float, category: str) -> Expense:
        return Expense(next(self._ids), description, round(amount, 2), category, _today())


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()
