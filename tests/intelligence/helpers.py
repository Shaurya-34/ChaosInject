import asyncio


class Req:
    """Stand-in for the API's transient QueryRequest (sql, parameters mapping, target_id)."""

    def __init__(self, sql, parameters=None, target_id="inventory-lab"):
        self.sql, self.parameters, self.target_id = sql, parameters if parameters is not None else {}, target_id

    def __repr__(self):
        return "Req(<redacted>)"


def run(coro):
    return asyncio.run(coro)
